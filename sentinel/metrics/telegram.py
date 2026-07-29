import asyncio
import time
from http import HTTPMethod
from typing import Any
from urllib.parse import urlsplit

from prometheus_client import CollectorRegistry, Counter, Histogram
from telegram.request import HTTPXRequest

from sentinel.metrics.constants import METRICS_NAMESPACE

TELEGRAM_OUTCOME_TRANSPORT_ERROR = "transport_error"
TELEGRAM_OUTCOME_CANCELLED = "cancelled"


class TelegramObserver:
    """No-op base class for optional Telegram transport observers."""

    def observe(self, method: str, outcome: str, started_at: float) -> None:
        pass


class TelegramMetrics(TelegramObserver):
    def __init__(self, registry: CollectorRegistry) -> None:
        self.requests = Counter(
            "requests",
            "Telegram Bot API HTTP attempts.",
            ("method", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="telegram",
            registry=registry,
        )
        self.request_duration = Histogram(
            "request_duration_seconds",
            "Telegram Bot API HTTP attempt duration.",
            ("method", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="telegram",
            registry=registry,
        )

    def observe(self, method: str, outcome: str, started_at: float) -> None:
        labels = {"method": method, "outcome": outcome}
        self.requests.labels(**labels).inc()
        self.request_duration.labels(**labels).observe(max(time.perf_counter() - started_at, 0.0))


class MetricsHTTPXRequest(HTTPXRequest):
    """Instrument PTB's public HTTP transport extension point."""

    def __init__(self, observer: TelegramObserver, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._observer = observer

    async def do_request(
        self,
        url: str,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[int, bytes]:
        metric_method = self._metric_method(url, method)
        started_at = time.perf_counter()
        outcome = TELEGRAM_OUTCOME_TRANSPORT_ERROR
        try:
            status, payload = await super().do_request(
                url,
                method,
                *args,
                **kwargs,
            )
        except asyncio.CancelledError:
            outcome = TELEGRAM_OUTCOME_CANCELLED
            raise
        except Exception:
            outcome = TELEGRAM_OUTCOME_TRANSPORT_ERROR
            raise
        else:
            outcome = self._status_outcome(status)
            return status, payload
        finally:
            self._observer.observe(metric_method, outcome, started_at)

    @staticmethod
    def _metric_method(url: str, http_method: str) -> str:
        if http_method.upper() != HTTPMethod.POST:
            return "file_download"
        return urlsplit(url).path.rsplit("/", 1)[-1] or "unknown"

    @staticmethod
    def _status_outcome(status: int) -> str:
        return f"{status // 100}xx" if 100 <= status < 600 else "other"
