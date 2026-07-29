import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from web3.middleware.base import Web3Middleware
from web3.types import RPCEndpoint, RPCResponse

from sentinel.metrics.constants import METRICS_NAMESPACE

SUCCESS = "success"
RPC_REJECTED = "rpc_rejected"
TRANSPORT_ERROR = "transport_error"
CANCELLED = "cancelled"


class RpcObserver:
    """No-op base class for optional RPC lifecycle observers."""

    def endpoint_connected(self, role: str, endpoint: str) -> None:
        pass

    def endpoint_disconnected(self, role: str) -> None:
        pass

    def endpoint_failed(self, role: str, endpoint: str, kind: str) -> None:
        pass

    def persistent_request_failed(self, role: str, method: str, outcome: str) -> None:
        pass


NOOP_RPC_OBSERVER = RpcObserver()


@dataclass(frozen=True, slots=True)
class _PersistentAttempt:
    role: str
    method: str
    endpoint: str
    started_at: float


_persistent_attempt: ContextVar[_PersistentAttempt | None] = ContextVar(
    "rpc_metrics_persistent_attempt", default=None
)


class RpcMetrics(RpcObserver):
    def __init__(self, registry: CollectorRegistry) -> None:
        self.attempts = Counter(
            "attempts",
            "RPC attempts made against an active endpoint.",
            ("role", "method", "endpoint", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="rpc",
            registry=registry,
        )
        self.attempt_duration = Histogram(
            "attempt_duration_seconds",
            "Duration of individual RPC attempts.",
            ("role", "method", "endpoint", "outcome"),
            namespace=METRICS_NAMESPACE,
            subsystem="rpc",
            registry=registry,
        )
        self.endpoint_failures = Counter(
            "endpoint_failures",
            "RPC endpoint failures observed by the fallback layer.",
            ("role", "endpoint", "kind"),
            namespace=METRICS_NAMESPACE,
            subsystem="rpc",
            registry=registry,
        )
        self.endpoint_switches = Counter(
            "endpoint_switches",
            "RPC endpoint switches after an active endpoint changed.",
            ("role", "from_endpoint", "to_endpoint"),
            namespace=METRICS_NAMESPACE,
            subsystem="rpc",
            registry=registry,
        )
        self.active_endpoint = Gauge(
            "active_endpoint",
            "Whether an RPC endpoint is currently active.",
            ("role", "endpoint"),
            namespace=METRICS_NAMESPACE,
            subsystem="rpc",
            registry=registry,
        )
        self._active_by_role: dict[str, str] = {}
        self._last_by_role: dict[str, str] = {}
        self._known_by_role: dict[str, set[str]] = {}

    def observe_attempt(
        self, role: str, method: str, endpoint: str, outcome: str, started_at: float
    ) -> None:
        labels = {
            "role": role,
            "method": method,
            "endpoint": endpoint,
            "outcome": outcome,
        }
        self.attempts.labels(**labels).inc()
        self.attempt_duration.labels(**labels).observe(max(time.perf_counter() - started_at, 0.0))

    def endpoint_connected(self, role: str, endpoint: str) -> None:
        previous = self._last_by_role.get(role)
        known = self._known_by_role.setdefault(role, set())
        known.add(endpoint)
        for endpoint_id in known:
            self.active_endpoint.labels(role=role, endpoint=endpoint_id).set(
                int(endpoint_id == endpoint)
            )
        if previous is not None and previous != endpoint:
            self.endpoint_switches.labels(
                role=role, from_endpoint=previous, to_endpoint=endpoint
            ).inc()
        self._active_by_role[role] = endpoint
        self._last_by_role[role] = endpoint

    def endpoint_disconnected(self, role: str) -> None:
        endpoint = self._active_by_role.pop(role, None)
        if endpoint is not None:
            self.active_endpoint.labels(role=role, endpoint=endpoint).set(0)

    def endpoint_failed(self, role: str, endpoint: str, kind: str) -> None:
        self.endpoint_failures.labels(role=role, endpoint=endpoint, kind=kind).inc()

    def persistent_request_failed(self, role: str, method: str, outcome: str) -> None:
        attempt = _persistent_attempt.get()
        if attempt is None:
            return
        _persistent_attempt.set(None)
        if outcome == "transport":
            outcome = TRANSPORT_ERROR
        self.observe_attempt(role, method, attempt.endpoint, outcome, attempt.started_at)


class RpcMetricsMiddleware(Web3Middleware):
    """Measure RPC attempts at web3.py's request/response boundary."""

    def __init__(self, w3: Any) -> None:
        super().__init__(w3)
        self._metrics: RpcMetrics = w3.provider.observer
        self._role: str = w3.provider.role
        self._provider = w3.provider
        self._persistent = bool(getattr(w3.provider, "has_persistent_connection", False))

    def _endpoint(self) -> str:
        endpoint = getattr(self._provider, "active_endpoint", None)
        return "not-connected" if endpoint is None else endpoint.metric_label

    async def async_wrap_make_request(self, make_request: Callable[..., Any]) -> Callable[..., Any]:
        async def middleware(method: RPCEndpoint, params: Any) -> RPCResponse:
            started_at = time.perf_counter()
            endpoint = self._endpoint()
            try:
                response = await make_request(method, params)
            except asyncio.CancelledError:
                outcome = CANCELLED
                raise
            except Exception:
                outcome = TRANSPORT_ERROR
                raise
            else:
                outcome = RPC_REJECTED if "error" in response else SUCCESS
                return response
            finally:
                self._metrics.observe_attempt(
                    self._role, str(method), endpoint, outcome, started_at
                )

        return middleware

    async def async_request_processor(
        self, method: RPCEndpoint, params: Any
    ) -> tuple[RPCEndpoint, Any]:
        if self._persistent:
            _persistent_attempt.set(
                _PersistentAttempt(self._role, str(method), self._endpoint(), time.perf_counter())
            )
        return method, params

    async def async_response_processor(
        self, method: RPCEndpoint, response: RPCResponse
    ) -> RPCResponse:
        if self._persistent:
            attempt = _persistent_attempt.get()
            if attempt is not None:
                _persistent_attempt.set(None)
                outcome = RPC_REJECTED if "error" in response else SUCCESS
                self._metrics.observe_attempt(
                    self._role,
                    str(method),
                    attempt.endpoint,
                    outcome,
                    attempt.started_at,
                )
        return response
