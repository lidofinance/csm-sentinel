import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from sentinel.metrics.constants import METRICS_NAMESPACE

AGGREGATION_OUTCOME_SUCCESS = "success"
AGGREGATION_OUTCOME_RETRY = "retry"
AGGREGATION_OUTCOME_ERROR = "error"
AGGREGATION_OUTCOME_CANCELLED = "cancelled"

T = TypeVar("T")
AggregationOperation = Callable[[], Coroutine[Any, Any, T | None]]


class AggregationObserver:
    """No-op base class for optional aggregation observers."""

    def observe(self, group: str, outcome: str, started_at: float) -> None:
        pass


class AggregationMetrics(AggregationObserver):
    def __init__(self, registry: CollectorRegistry) -> None:
        self.pending_windows = Gauge(
            "pending_windows",
            "Persisted aggregation windows awaiting completion.",
            namespace=METRICS_NAMESPACE,
            subsystem="aggregation",
            registry=registry,
        )
        self.runs = Counter(
            "runs",
            "Aggregation window executions.",
            ("group", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="aggregation",
            registry=registry,
        )
        self.duration = Histogram(
            "duration_seconds",
            "Aggregation window execution duration.",
            ("group", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="aggregation",
            registry=registry,
        )

    def bind(self, pending_windows: Callable[[], int]) -> None:
        self.pending_windows.set_function(pending_windows)

    def observe(self, group: str, outcome: str, started_at: float) -> None:
        labels = {"group": group, "outcome": outcome}
        self.runs.labels(**labels).inc()
        self.duration.labels(**labels).observe(max(time.perf_counter() - started_at, 0.0))


class AggregationMetricsMiddleware:
    """Measure aggregation operations without coupling their implementation to Prometheus."""

    def __init__(self, observer: AggregationObserver) -> None:
        self._observer = observer

    async def run(self, group: str, operation: AggregationOperation[T]) -> T | None:
        started_at = time.perf_counter()
        try:
            result = await operation()
        except asyncio.CancelledError:
            outcome = AGGREGATION_OUTCOME_CANCELLED
            raise
        except Exception:
            outcome = AGGREGATION_OUTCOME_ERROR
            raise
        else:
            outcome = AGGREGATION_OUTCOME_RETRY if result is None else AGGREGATION_OUTCOME_SUCCESS
            return result
        finally:
            self._observer.observe(group, outcome, started_at)
