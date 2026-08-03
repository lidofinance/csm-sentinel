import asyncio
import time
from collections.abc import Awaitable, Callable

import aiohttp
from prometheus_client import CollectorRegistry, Counter, Histogram

from sentinel.metrics.constants import METRICS_NAMESPACE

HTTP_OUTCOME_TRANSPORT_ERROR = "transport_error"
HTTP_OUTCOME_CANCELLED = "cancelled"

HttpHandler = Callable[[aiohttp.ClientRequest], Awaitable[aiohttp.ClientResponse]]


class HttpMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.requests = Counter(
            "requests",
            "Outgoing HTTP requests.",
            ("host", "method", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="http",
            registry=registry,
        )
        self.request_duration = Histogram(
            "request_duration_seconds",
            "Outgoing HTTP request duration.",
            ("host", "method", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="http",
            registry=registry,
        )

    def observe(
        self,
        host: str,
        method: str,
        outcome: str,
        started_at: float,
    ) -> None:
        labels = {"host": host, "method": method, "outcome": outcome}
        self.requests.labels(**labels).inc()
        self.request_duration.labels(**labels).observe(max(time.perf_counter() - started_at, 0.0))


class HttpMetricsMiddleware:
    """Instrument aiohttp requests at its native client middleware boundary."""

    def __init__(self, metrics: HttpMetrics) -> None:
        self._metrics = metrics

    async def __call__(
        self,
        request: aiohttp.ClientRequest,
        handler: HttpHandler,
    ) -> aiohttp.ClientResponse:
        started_at = time.perf_counter()
        outcome = HTTP_OUTCOME_TRANSPORT_ERROR
        try:
            response = await handler(request)
        except asyncio.CancelledError:
            outcome = HTTP_OUTCOME_CANCELLED
            raise
        except Exception:
            outcome = HTTP_OUTCOME_TRANSPORT_ERROR
            raise
        else:
            outcome = self._status_outcome(response.status)
            return response
        finally:
            self._metrics.observe(
                request.url.host or "unknown",
                request.method.upper(),
                outcome,
                started_at,
            )

    @staticmethod
    def _status_outcome(status: int) -> str:
        return f"{status // 100}xx" if 100 <= status < 600 else "other"
