from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from sentinel.metrics.rpc import RpcMetrics, RpcMetricsMiddleware
from sentinel.rpc_provider import FallbackAsyncWeb3, FallbackRequestProvider, RpcEndpointPool


def _value(registry: CollectorRegistry, name: str, labels: dict[str, str]) -> float | None:
    return registry.get_sample_value(name, labels)


def test_middleware_can_be_registered_by_class():
    provider = FallbackRequestProvider(
        RpcEndpointPool(("ws://primary.invalid",)),
        role="reads",
        observer=RpcMetrics(CollectorRegistry()),
    )
    w3 = FallbackAsyncWeb3(provider)

    w3.middleware_onion.inject(RpcMetricsMiddleware, name="rpc_metrics", layer=0)


@pytest.mark.asyncio
async def test_middleware_records_non_persistent_attempt():
    registry = CollectorRegistry()
    metrics = RpcMetrics(registry)
    w3 = SimpleNamespace(
        provider=SimpleNamespace(
            observer=metrics,
            role="reads",
            has_persistent_connection=False,
            active_endpoint=SimpleNamespace(metric_label="primary.example"),
        )
    )
    middleware = RpcMetricsMiddleware(w3)
    wrapped = await middleware.async_wrap_make_request(
        AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": "0x1"})
    )

    await wrapped("eth_chainId", [])

    assert _value(
        registry,
        "sentinel_rpc_attempts_total",
        {
            "role": "reads",
            "method": "eth_chainId",
            "endpoint": "primary.example",
            "outcome": "success",
        },
    ) == pytest.approx(1)


@pytest.mark.asyncio
async def test_persistent_failure_finishes_attempt():
    registry = CollectorRegistry()
    metrics = RpcMetrics(registry)
    w3 = SimpleNamespace(
        provider=SimpleNamespace(
            observer=metrics,
            role="subscription",
            has_persistent_connection=True,
            active_endpoint=SimpleNamespace(metric_label="subscription.example"),
        )
    )
    middleware = RpcMetricsMiddleware(w3)

    await middleware.async_request_processor("eth_subscribe", ["logs"])
    metrics.persistent_request_failed("subscription", "eth_subscribe", "transport")

    assert _value(
        registry,
        "sentinel_rpc_attempts_total",
        {
            "role": "subscription",
            "method": "eth_subscribe",
            "endpoint": "subscription.example",
            "outcome": "transport_error",
        },
    ) == pytest.approx(1)


@pytest.mark.asyncio
async def test_provider_lifecycle_updates_bounded_endpoint_metrics(monkeypatch):
    registry = CollectorRegistry()
    metrics = RpcMetrics(registry)
    pool = RpcEndpointPool(("ws://user:secret@primary.invalid", "ws://fallback.invalid"))
    provider = FallbackRequestProvider(
        pool,
        role="reads",
        max_connection_rounds=1,
        retry_interval_seconds=0,
        observer=metrics,
    )
    monkeypatch.setattr(provider, "_open_endpoint", AsyncMock())
    monkeypatch.setattr(provider, "_close_endpoint", AsyncMock())
    monkeypatch.setattr(provider, "is_connected", AsyncMock(return_value=False))

    await provider.connect()
    await provider._invalidate_endpoint(  # noqa: SLF001
        pool.endpoints[0], provider.connection_generation, cooldown=True
    )
    await provider.connect()

    assert _value(
        registry,
        "sentinel_rpc_endpoint_switches_total",
        {
            "role": "reads",
            "from_endpoint": "primary.invalid",
            "to_endpoint": "fallback.invalid",
        },
    ) == pytest.approx(1)
    assert _value(
        registry,
        "sentinel_rpc_active_endpoint",
        {"role": "reads", "endpoint": "fallback.invalid"},
    ) == pytest.approx(1)
    exposition = generate_latest(registry).decode()
    assert "secret" not in exposition
