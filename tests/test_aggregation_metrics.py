from unittest.mock import AsyncMock

import pytest
from prometheus_client import CollectorRegistry

from sentinel.metrics.aggregation import AggregationMetrics, AggregationMetricsMiddleware


def _value(registry: CollectorRegistry, name: str, labels=None) -> float | None:
    return registry.get_sample_value(name, labels)


@pytest.mark.asyncio
async def test_aggregation_middleware_records_success_and_retry():
    registry = CollectorRegistry()
    metrics = AggregationMetrics(registry)
    middleware = AggregationMetricsMiddleware(metrics)

    await middleware.run("deposits", AsyncMock(return_value=object()))
    await middleware.run("deposits", AsyncMock(return_value=None))

    assert (
        _value(
            registry,
            "sentinel_aggregation_runs_total",
            {"group": "deposits", "outcome": "success"},
        )
        == 1
    )
    assert (
        _value(
            registry,
            "sentinel_aggregation_runs_total",
            {"group": "deposits", "outcome": "retry"},
        )
        == 1
    )


def test_pending_windows_gauge_is_collected_from_bound_state():
    registry = CollectorRegistry()
    metrics = AggregationMetrics(registry)
    state = {"pending": 2}
    metrics.bind(lambda: state["pending"])

    assert _value(registry, "sentinel_aggregation_pending_windows") == 2

    state["pending"] = 5
    assert _value(registry, "sentinel_aggregation_pending_windows") == 5
