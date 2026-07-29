from collections.abc import Callable

from prometheus_client import CollectorRegistry, Counter, Gauge

from sentinel.app.health import HealthState
from sentinel.metrics.constants import METRICS_NAMESPACE


class ChainObserver:
    """No-op base class for optional chain lifecycle observers."""

    def subscription_recovered(self, reason: str) -> None:
        pass


NOOP_CHAIN_OBSERVER = ChainObserver()


class ChainMetrics(ChainObserver):
    def __init__(self, registry: CollectorRegistry) -> None:
        self.processed_block = Gauge(
            "processed_block",
            "Latest block committed to Sentinel processing state.",
            namespace=METRICS_NAMESPACE,
            subsystem="chain",
            registry=registry,
        )
        self.subscription_active = Gauge(
            "subscription_active",
            "Whether the live chain subscription is active.",
            namespace=METRICS_NAMESPACE,
            subsystem="chain",
            registry=registry,
        )
        self.catchup_active = Gauge(
            "catchup_active",
            "Whether historical catch-up or replay is active.",
            namespace=METRICS_NAMESPACE,
            subsystem="chain",
            registry=registry,
        )
        self.subscription_recoveries = Counter(
            "subscription_recoveries",
            "Live subscription recoveries that required rebuilding the runtime.",
            ("reason",),
            namespace=METRICS_NAMESPACE,
            subsystem="chain",
            registry=registry,
        )

    def bind(
        self,
        *,
        health: HealthState,
        processed_block: Callable[[], int],
    ) -> None:
        """Bind gauges to runtime state, evaluated when Prometheus scrapes."""

        self.processed_block.set_function(processed_block)
        self.subscription_active.set_function(lambda: int(health.snapshot().subscription_active))
        self.catchup_active.set_function(lambda: int(health.snapshot().catchup_active))

    def subscription_recovered(self, reason: str) -> None:
        self.subscription_recoveries.labels(reason=reason).inc()
