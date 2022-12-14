# SPDX-FileCopyrightText: 2022 Magenta ApS
# SPDX-License-Identifier: MPL-2.0
# pylint: disable=redefined-outer-name
# pylint: disable=too-many-arguments
"Test `engagement_updater.main`"
import asyncio
from datetime import datetime
from time import monotonic
from typing import Any
from typing import AsyncGenerator
from typing import Callable
from typing import cast
from typing import Generator
from typing import Set
from typing import Tuple
from unittest.mock import ANY
from unittest.mock import AsyncMock
from unittest.mock import call
from unittest.mock import MagicMock
from unittest.mock import patch
from uuid import UUID
from uuid import uuid4

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ramqp.mo import MOAMQPSystem
from ramqp.mo import MORouter
from ramqp.mo.models import ObjectType
from ramqp.mo.models import PayloadType
from ramqp.mo.models import RequestType
from ramqp.mo.models import ServiceType
from starlette.status import HTTP_200_OK
from starlette.status import HTTP_202_ACCEPTED

from engagement_updater.config import get_settings
from engagement_updater.config import Settings
from engagement_updater.main import build_information
from engagement_updater.main import construct_clients
from engagement_updater.main import create_app
from engagement_updater.main import gather_with_concurrency
from engagement_updater.main import update_build_information
from tests import ASSOCIATION_TYPE_USER_KEY


def get_metric_value(metric: Any, labels: Tuple[str]) -> float:
    """Get the value of a given metric with the given label-set.

    Args:
        metric: The metric to query.
        labels: The label-set to query with.

    Returns:
        The metric value.
    """
    # pylint: disable=protected-access
    metric = metric.labels(*labels)._value
    return cast(float, metric.get())


def clear_metric_value(metric: Any) -> None:
    """Get the value of a given metric with the given label-set.

    Args:
        metric: The metric to query.
        labels: The label-set to query with.

    Returns:
        The metric value.
    """
    metric.clear()


def get_metric_labels(metric: Any) -> Set[Tuple[str]]:
    """Get the label-set for a given metric.

    Args:
        metric: The metric to query.

    Returns:
        The label-set.
    """
    # pylint: disable=protected-access
    return set(metric._metrics.keys())


def test_build_information() -> None:
    """Test that build metrics are updated as expected."""
    clear_metric_value(build_information)
    assert build_information._value == {}  # pylint: disable=protected-access
    update_build_information("1.0.0", "cafebabe")
    assert build_information._value == {  # pylint: disable=protected-access
        "version": "1.0.0",
        "hash": "cafebabe",
    }


async def test_gather_with_concurrency() -> None:
    """Test gather with concurrency."""
    start = monotonic()
    await asyncio.gather(
        *[
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
        ]
    )
    end = monotonic()
    duration = end - start
    assert duration < 0.15

    start = monotonic()
    await gather_with_concurrency(
        3,
        *[
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
        ],
    )
    end = monotonic()
    duration = end - start
    assert duration < 0.15

    start = monotonic()
    await gather_with_concurrency(
        1,
        *[
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
            asyncio.sleep(0.1),
        ],
    )
    end = monotonic()
    duration = end - start
    assert duration > 0.3


@pytest.fixture
def fastapi_app_builder() -> Generator[Callable[..., FastAPI], None, None]:
    """Fixture for the FastAPI app builder."""

    def builder(*args: Any, default_args: bool = True, **kwargs: Any) -> FastAPI:
        if default_args:
            kwargs["client_secret"] = "hunter2"
            kwargs["expose_metrics"] = False
            kwargs["association_type"] = ASSOCIATION_TYPE_USER_KEY
        return create_app(*args, **kwargs)

    yield builder


@pytest.fixture
def fastapi_app(
    fastapi_app_builder: Callable[..., FastAPI]
) -> Generator[FastAPI, None, None]:
    """Fixture for the FastAPI app."""
    yield fastapi_app_builder(client_secret="hunter2", expose_metrics=False)


@pytest.fixture
def test_client_builder(
    fastapi_app_builder: Callable[..., FastAPI]
) -> Generator[Callable[..., TestClient], None, None]:
    """Fixture for the FastAPI test client builder."""

    def builder(*args: Any, **kwargs: Any) -> TestClient:
        return TestClient(fastapi_app_builder(*args, **kwargs))

    yield builder


@pytest.fixture
def test_client(
    test_client_builder: Callable[..., TestClient]
) -> Generator[TestClient, None, None]:
    """Fixture for the FastAPI test client."""
    yield test_client_builder(client_secret="hunter2", expose_metrics=False)


async def test_root_endpoint(test_client: TestClient) -> None:
    """Test the root endpoint on our app."""
    response = test_client.get("/")
    assert response.status_code == 200
    assert response.json() == {"name": "engagement_updater"}


async def test_metrics_endpoint(test_client_builder: Callable[..., TestClient]) -> None:
    """Test the metrics endpoint on our app."""
    test_client = test_client_builder(
        default_args=False,
        client_secret="hunter2",
        association_type=ASSOCIATION_TYPE_USER_KEY,
    )
    response = test_client.get("/metrics")
    assert response.status_code == 200


@patch("engagement_updater.main.handle_engagement_update")
async def test_trigger_all_endpoint(
    handle_engagement_update: MagicMock,
    test_client_builder: Callable[..., TestClient],
) -> None:
    """Test the trigger all endpoint on our app."""
    # Arrange: mock `get_bulk_update_payloads` return value
    async def _async_generator(item: Any) -> AsyncGenerator:
        yield item

    # Arrange: context
    gql_client = AsyncMock()
    model_client = AsyncMock()
    settings = MagicMock(spec=Settings)
    context = {
        "gql_client": gql_client,
        "model_client": model_client,
        "settings": settings,
    }

    # Arrange: mock payload returned by `get_bulk_update_payloads`
    payload = PayloadType(
        uuid=uuid4(),
        object_uuid=uuid4(),
        time=datetime.now(),
    )

    # Act
    with patch("engagement_updater.main.construct_context", return_value=context):
        test_client = test_client_builder()
        with patch(
            "engagement_updater.main.get_bulk_update_payloads",
            return_value=_async_generator(payload),
        ) as get_bulk_update_payloads:
            response = test_client.post("/trigger/all")

    # Assert
    assert response.status_code == HTTP_202_ACCEPTED
    assert response.json() == {"status": "Background job triggered"}
    get_bulk_update_payloads.assert_called_once_with(gql_client)
    handle_engagement_update.assert_called_once_with(
        gql_client,
        model_client,
        ANY,
        ANY,
        payload,
    )


@patch("engagement_updater.main.handle_engagement_update")
async def test_trigger_uuid_endpoint(
    handle_engagement_update: MagicMock,
    test_client_builder: Callable[..., TestClient],
) -> None:
    """Test the trigger uuid endpoint on our app."""
    # Arrange: mock `get_single_update_payload` return value
    async def _async_generator(item: Any) -> AsyncGenerator:
        yield item

    # Arrange: context
    gql_client = AsyncMock()
    model_client = AsyncMock()
    settings = MagicMock(spec=Settings)
    context = {
        "gql_client": gql_client,
        "model_client": model_client,
        "settings": settings,
    }

    # Arrange: mock payload returned by `get_single_update_payload`
    engagement_uuid: UUID = uuid4()
    payload = PayloadType(
        uuid=uuid4(),  # employee UUID
        object_uuid=engagement_uuid,
        time=datetime.now(),
    )

    # Act
    with patch("engagement_updater.main.construct_context", return_value=context):
        test_client = test_client_builder()
        with patch(
            "engagement_updater.main.get_single_update_payload",
            return_value=_async_generator(payload),
        ) as get_single_update_payload:
            response = test_client.post(f"/trigger/{str(engagement_uuid)}")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert response.json() == {"status": "OK"}
    get_single_update_payload.assert_called_once_with(
        gql_client,
        engagement_uuid,
    )
    handle_engagement_update.assert_called_once_with(
        gql_client,
        model_client,
        ANY,
        ANY,
        payload,
    )


@patch("engagement_updater.main.MOAMQPSystem")
@patch("engagement_updater.main.MORouter")
async def test_lifespan(
    mo_router: MORouter,
    mo_amqpsystem: MOAMQPSystem,
    fastapi_app: FastAPI,
) -> None:
    """Test that our lifespan events are handled as expected."""
    amqp_system = MagicMock()
    amqp_system.start = AsyncMock()
    amqp_system.stop = AsyncMock()

    mo_amqpsystem.return_value = amqp_system  # type: ignore

    router = MagicMock()
    mo_router.return_value = router  # type: ignore

    assert not amqp_system.mock_calls

    # Fire startup event on entry, and shutdown on exit
    async with LifespanManager(fastapi_app):
        assert len(router.mock_calls) == 2
        # Create register calls
        assert router.mock_calls[0] == call.register(
            ServiceType.EMPLOYEE, ObjectType.ENGAGEMENT, RequestType.WILDCARD
        )
        # Register calls
        assert router.mock_calls[1]

        # Clean mock to only capture shutdown changes
        amqp_system.reset_mock()


async def test_liveness_endpoint(test_client: TestClient) -> None:
    """Test the liveness endpoint on our app."""
    response = test_client.get("/health/live")
    assert response.status_code == 204


@pytest.mark.parametrize(
    "amqp_ok,gql_ok,model_ok,expected",
    [
        (True, True, True, 204),
        (False, True, True, 503),
        (True, False, True, 503),
        (True, True, False, 503),
        (True, False, False, 503),
        (False, True, False, 503),
        (False, False, True, 503),
        (False, False, False, 503),
    ],
)
@patch("engagement_updater.main.construct_context")
async def test_readiness_endpoint(
    construct_context: MagicMock,
    test_client_builder: Callable[..., TestClient],
    amqp_ok: bool,
    gql_ok: bool,
    model_ok: bool,
    expected: int,
) -> None:
    """Test the readiness endpoint handles errors."""
    gql_client = AsyncMock()
    if gql_ok:
        gql_client.execute.return_value = {
            "org": {"uuid": "35304fa6-ff84-4ea4-aac9-a285995ab45b"}
        }
    else:
        gql_client.execute.return_value = {
            "errors": [{"message": "Something went wrong"}]
        }

    model_client_response = MagicMock()
    if model_ok:
        model_client_response.json.return_value = [
            {"uuid": "35304fa6-ff84-4ea4-aac9-a285995ab45b"}
        ]
    else:
        model_client_response.json.return_value = "BOOM"
    model_client = AsyncMock()
    model_client.get.return_value = model_client_response

    amqp_system = MagicMock()
    amqp_system.healthcheck.return_value = amqp_ok

    construct_context.return_value = {
        "gql_client": gql_client,
        "model_client": model_client,
        "amqp_system": amqp_system,
    }
    test_client = test_client_builder()

    response = test_client.get("/health/ready")
    assert response.status_code == expected

    assert len(gql_client.execute.mock_calls) == 1
    assert model_client.mock_calls == [call.get("/service/o/"), call.get().json()]
    assert amqp_system.mock_calls == [call.healthcheck()]


@pytest.mark.parametrize(
    "amqp_ok,gql_ok,model_ok,expected",
    [
        (True, True, True, 204),
        (False, True, True, 503),
        (True, False, True, 503),
        (True, True, False, 503),
        (True, False, False, 503),
        (False, True, False, 503),
        (False, False, True, 503),
        (False, False, False, 503),
    ],
)
@patch("engagement_updater.main.construct_context")
async def test_readiness_endpoint_exception(
    construct_context: MagicMock,
    test_client_builder: Callable[..., TestClient],
    amqp_ok: bool,
    gql_ok: bool,
    model_ok: bool,
    expected: int,
) -> None:
    """Test the readiness endpoint handled exceptions nicely."""
    gql_client = AsyncMock()
    if gql_ok:
        gql_client.execute.return_value = {
            "org": {"uuid": "35304fa6-ff84-4ea4-aac9-a285995ab45b"}
        }
    else:
        gql_client.execute.side_effect = ValueError("BOOM")

    model_client_response = MagicMock()
    if model_ok:
        model_client_response.json.return_value = [
            {"uuid": "35304fa6-ff84-4ea4-aac9-a285995ab45b"}
        ]
    else:
        model_client_response.json.side_effect = ValueError("BOOM")
    model_client = AsyncMock()
    model_client.get.return_value = model_client_response

    amqp_system = MagicMock()
    if amqp_ok:
        amqp_system.healthcheck.return_value = True
    else:
        amqp_system.healthcheck.side_effect = ValueError("BOOM")

    construct_context.return_value = {
        "gql_client": gql_client,
        "model_client": model_client,
        "amqp_system": amqp_system,
    }
    test_client = test_client_builder()

    response = test_client.get("/health/ready")
    assert response.status_code == expected


@patch("engagement_updater.main.PersistentGraphQLClient")
def test_gql_client_created_with_timeout(mock_gql_client: MagicMock) -> None:
    """Test that PersistentGraphQLClient is called with timeout setting"""

    # Arrange
    settings = get_settings(
        client_secret="not used",
        association_type=ASSOCIATION_TYPE_USER_KEY,
        graphql_timeout=15,
    )

    # Act
    construct_clients(settings)

    # Assert
    assert 15 == mock_gql_client.call_args.kwargs["httpx_client_kwargs"]["timeout"]
    assert 15 == mock_gql_client.call_args.kwargs["execute_timeout"]
