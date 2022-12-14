# SPDX-FileCopyrightText: 2022 Magenta ApS
# SPDX-License-Identifier: MPL-2.0
"""Event handling."""
from asyncio import gather
from asyncio import Semaphore
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack
from functools import partial
from typing import Any
from typing import AsyncGenerator
from typing import Awaitable
from typing import Callable
from typing import Coroutine
from typing import Tuple
from typing import TypeVar
from uuid import UUID

import structlog
from fastapi import BackgroundTasks
from fastapi import FastAPI
from fastapi import Query
from fastapi import Response
from gql import gql
from more_itertools import one
from prometheus_client import Info
from prometheus_fastapi_instrumentator import Instrumentator
from raclients.graph.client import PersistentGraphQLClient
from raclients.modelclient.mo import ModelClient
from ramqp.mo import MOAMQPSystem
from ramqp.mo import MORouter
from ramqp.mo.models import MORoutingKey
from ramqp.mo.models import ObjectType
from ramqp.mo.models import PayloadType
from ramqp.mo.models import RequestType
from ramqp.mo.models import ServiceType
from ramqp.utils import sleep_on_error
from starlette.status import HTTP_202_ACCEPTED
from starlette.status import HTTP_204_NO_CONTENT
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from .config import get_settings
from .config import Settings
from .handler import get_bulk_update_payloads
from .handler import get_single_update_payload
from .handler import handle_engagement_update
from .handler import ResultType


logger = structlog.get_logger()

T = TypeVar("T")

build_information = Info("build_information", "Build information")


def update_build_information(version: str, build_hash: str) -> None:
    """Update build information.

    Args:
        version: The version to set.
        build_hash: The build hash to set.

    Returns:
        None.
    """
    build_information.info(
        {
            "version": version,
            "hash": build_hash,
        }
    )


async def healthcheck_gql(gql_client: PersistentGraphQLClient) -> bool:
    """Check that our GraphQL connection is healthy.

    Args:
        gql_client: The GraphQL client to check health of.

    Returns:
        Whether the client is healthy or not.
    """
    query = gql(
        """
        query HealthcheckQuery {
            org {
                uuid
            }
        }
        """
    )
    try:
        result = await gql_client.execute(query)
        if result["org"]["uuid"]:
            return True
    except Exception:  # pylint: disable=broad-except
        logger.exception("Exception occured during GraphQL healthcheck")
    return False


async def healthcheck_model_client(model_client: ModelClient) -> bool:
    """Check that our ModelClient connection is healthy.

    Args:
        model_client: The MO Model client to check health of.

    Returns:
        Whether the client is healthy or not.
    """
    try:
        response = await model_client.get("/service/o/")
        result = response.json()
        if one(result)["uuid"]:
            return True
    except Exception:  # pylint: disable=broad-except
        logger.exception("Exception occured during GraphQL healthcheck")
    return False


def construct_clients(
    settings: Settings,
) -> Tuple[PersistentGraphQLClient, ModelClient]:
    """Construct clients froms settings.

    Args:
        settings: Integration settings module.

    Returns:
        Tuple with PersistentGraphQLClient and ModelClient.
    """
    gql_client = PersistentGraphQLClient(
        url=settings.mo_url + "/graphql/v2",
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        auth_server=settings.auth_server,
        auth_realm=settings.auth_realm,
        execute_timeout=settings.graphql_timeout,
        httpx_client_kwargs={"timeout": settings.graphql_timeout},
    )
    model_client = ModelClient(
        base_url=settings.mo_url,
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        auth_server=settings.auth_server,
        auth_realm=settings.auth_realm,
    )
    return gql_client, model_client


def configure_logging(settings: Settings) -> None:
    """Setup our logging.

    Args:
        settings: Integration settings module.

    Returns:
        None
    """
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(settings.log_level.value)
    )


async def gather_with_concurrency(parallel: int, *tasks: Awaitable[T]) -> list[T]:
    """Asyncio gather, but with limited concurrency.

    Args:
        parallel: The number of concurrent tasks being executed.
        tasks: List of tasks to execute.

    Returns:
        List of return values from awaiting the tasks.
    """
    semaphore = Semaphore(parallel)

    async def semaphore_task(task: Awaitable[T]) -> T:
        async with semaphore:
            return await task

    return await gather(*map(semaphore_task, tasks))


def construct_context() -> dict[str, Any]:
    """Construct request context."""
    return {}


def create_app(  # pylint: disable=too-many-statements
    *args: Any, **kwargs: Any
) -> FastAPI:
    """FastAPI application factory.

    Starts the metrics server, then listens to AMQP messages forever.

    Returns:
        None
    """
    settings = get_settings(*args, **kwargs)
    configure_logging(settings)

    app = FastAPI()

    update_build_information(
        version=settings.commit_tag, build_hash=settings.commit_sha
    )

    if settings.expose_metrics:
        logger.info("Starting metrics server")
        Instrumentator().instrument(app).expose(app)

    context = construct_context()

    context["settings"] = settings

    # pylint: disable=unused-argument
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator:
        async with AsyncExitStack() as stack:
            logger.info("Setting up clients")
            gql_client, model_client = construct_clients(settings)
            gql_client = context["gql_client"] = await stack.enter_async_context(
                gql_client
            )
            model_client = context["model_client"] = await stack.enter_async_context(
                model_client
            )

            logger.info("Setting up AMQP system")

            router = MORouter()
            amqp_system = MOAMQPSystem(settings=settings.amqp, router=router)

            @sleep_on_error
            @router.register(
                ServiceType.EMPLOYEE, ObjectType.ENGAGEMENT, RequestType.WILDCARD
            )
            async def on_amqp_message(
                mo_routing_key: MORoutingKey,
                payload: PayloadType,
                **_: Any,
            ) -> ResultType:
                return await handle_engagement_update(
                    gql_client,
                    model_client,
                    settings,
                    mo_routing_key,
                    payload,
                )

            context["amqp_system"] = amqp_system

            logger.info("Starting AMQP system")
            await stack.enter_async_context(amqp_system)

            # Yield to keep the AMQP system open until the ASGI application is closed.
            # Control will be returned to here when the ASGI application is shut down.
            yield

    app.router.lifespan_context = lifespan

    @app.get("/")
    async def index() -> dict[str, str]:
        return {"name": "engagement_updater"}

    @app.post("/trigger/all", status_code=HTTP_202_ACCEPTED)
    async def bulk_update(background_tasks: BackgroundTasks) -> dict[str, str]:
        """Call `handle_engagement_update` on all engagements"""
        gql_client: PersistentGraphQLClient = context["gql_client"]
        handle = _get_curried_handle_engagement_update(context)
        # Create tasks for `handle(payload)` (which is equivalent to
        # `handle_engagement_update(..., payload)`.)
        async for payload in get_bulk_update_payloads(gql_client):
            background_tasks.add_task(handle, payload)  # type: ignore
        return {"status": "Background job triggered"}

    @app.post("/trigger/{uuid}")
    async def single_update(
        uuid: UUID = Query(..., description="UUID of the engagement to process"),
    ) -> dict[str, str]:
        """Call `handle_engagement_update` on specific engagement"""
        gql_client: PersistentGraphQLClient = context["gql_client"]
        handle = _get_curried_handle_engagement_update(context)
        # Iterate over the expected single payload, and call the curried version of
        # `handle_engagement_update` for that single payload.
        async for payload in get_single_update_payload(gql_client, uuid):
            await handle(payload)  # type: ignore
        return {"status": "OK"}

    @app.get("/health/live", status_code=HTTP_204_NO_CONTENT)
    async def liveness() -> None:
        """Endpoint to be used as a liveness probe for Kubernetes."""
        return None

    @app.get(
        "/health/ready",
        status_code=HTTP_204_NO_CONTENT,
        responses={
            "204": {"description": "Ready"},
            "503": {"description": "Not ready"},
        },
    )
    async def readiness(response: Response) -> Response:
        """Endpoint to be used as a readiness probe for Kubernetes."""
        response.status_code = HTTP_204_NO_CONTENT

        healthchecks = {}
        try:
            # Check AMQP connection
            healthchecks["AMQP"] = context["amqp_system"].healthcheck()
            # Check GraphQL connection (gql_client)
            healthchecks["GraphQL"] = await healthcheck_gql(context["gql_client"])
            # Check Service API connection (model_client)
            healthchecks["Service API"] = await healthcheck_model_client(
                context["model_client"]
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception("Exception occured during readiness probe")
            response.status_code = HTTP_503_SERVICE_UNAVAILABLE

        for name, ready in healthchecks.items():
            if not ready:
                logger.warn(f"{name} is not ready")

        if not all(healthchecks.values()):
            response.status_code = HTTP_503_SERVICE_UNAVAILABLE

        return response

    return app


def _get_curried_handle_engagement_update(
    context: dict,
) -> Callable[
    [PersistentGraphQLClient, ModelClient, Settings, MORoutingKey],
    Coroutine[Any, Any, ResultType],
]:
    gql_client: PersistentGraphQLClient = context["gql_client"]
    model_client: ModelClient = context["model_client"]
    settings: Settings = context["settings"]
    mo_routing_key: MORoutingKey = MORoutingKey.from_tuple(
        (ServiceType.EMPLOYEE, ObjectType.ENGAGEMENT, RequestType.EDIT)
    )
    # Create curried version of `handle_engagement_update` which only requires the
    # final `payload` argument.
    return partial(
        handle_engagement_update,
        gql_client,
        model_client,
        settings,
        mo_routing_key,
    )
