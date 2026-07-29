import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from sentinel.metrics.constants import METRICS_NAMESPACE

JOB_OUTCOME_SUCCESS = "success"
JOB_OUTCOME_ERROR = "error"
JOB_OUTCOME_CANCELLED = "cancelled"

JobCallback = Callable[[Any], Coroutine[Any, Any, bool | None]]


class JobObserver:
    """No-op base class for optional scheduled-job observers."""

    def observe(self, job: str, outcome: str, started_at: float) -> None:
        pass


NOOP_JOB_OBSERVER = JobObserver()


class JobMetrics(JobObserver):
    def __init__(self, registry: CollectorRegistry) -> None:
        self.runs = Counter(
            "runs",
            "Scheduled job executions.",
            ("job", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="jobs",
            registry=registry,
        )
        self.duration = Histogram(
            "duration_seconds",
            "Scheduled job execution duration.",
            ("job", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="jobs",
            registry=registry,
        )
        self.last_success = Gauge(
            "last_success_timestamp_seconds",
            "Unix timestamp of the latest successful scheduled job execution.",
            ("job",),
            namespace=METRICS_NAMESPACE,
            subsystem="jobs",
            registry=registry,
        )

    def observe(self, job: str, outcome: str, started_at: float) -> None:
        labels = {"job": job, "outcome": outcome}
        self.runs.labels(**labels).inc()
        self.duration.labels(**labels).observe(max(time.perf_counter() - started_at, 0.0))
        if outcome == JOB_OUTCOME_SUCCESS:
            self.last_success.labels(job=job).set_to_current_time()


class JobMetricsMiddleware:
    """Wrap scheduled callbacks with consistent execution metrics."""

    def __init__(self, observer: JobObserver) -> None:
        self._observer = observer

    def wrap(self, job: str, callback: JobCallback) -> JobCallback:
        async def measured(context: Any) -> bool | None:
            started_at = time.perf_counter()
            try:
                result = await callback(context)
            except asyncio.CancelledError:
                outcome = JOB_OUTCOME_CANCELLED
                raise
            except Exception:
                outcome = JOB_OUTCOME_ERROR
                raise
            else:
                outcome = JOB_OUTCOME_ERROR if result is False else JOB_OUTCOME_SUCCESS
                return result
            finally:
                self._observer.observe(job, outcome, started_at)

        return measured


NOOP_JOB_MIDDLEWARE = JobMetricsMiddleware(NOOP_JOB_OBSERVER)
