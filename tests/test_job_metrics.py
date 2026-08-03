from unittest.mock import AsyncMock

import pytest
from prometheus_client import CollectorRegistry

from sentinel.metrics.jobs import JobMetrics, JobMetricsMiddleware


def _value(registry: CollectorRegistry, name: str, labels: dict[str, str]) -> float | None:
    return registry.get_sample_value(name, labels)


@pytest.mark.asyncio
async def test_job_middleware_records_success_and_last_success():
    registry = CollectorRegistry()
    middleware = JobMetricsMiddleware(JobMetrics(registry))
    callback = AsyncMock(return_value=None)

    await middleware.wrap("chain_head_poll", callback)(object())

    assert (
        _value(
            registry,
            "sentinel_jobs_runs_total",
            {"job": "chain_head_poll", "outcome": "success"},
        )
        == 1
    )
    assert (
        _value(
            registry,
            "sentinel_jobs_last_success_timestamp_seconds",
            {"job": "chain_head_poll"},
        )
        is not None
    )


@pytest.mark.asyncio
async def test_job_middleware_records_recoverable_false_result_as_error():
    registry = CollectorRegistry()
    middleware = JobMetricsMiddleware(JobMetrics(registry))

    result = await middleware.wrap("chain_head_poll", AsyncMock(return_value=False))(object())

    assert result is False
    assert (
        _value(
            registry,
            "sentinel_jobs_runs_total",
            {"job": "chain_head_poll", "outcome": "error"},
        )
        == 1
    )
