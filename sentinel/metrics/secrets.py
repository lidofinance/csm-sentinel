from prometheus_client import CollectorRegistry, Gauge

from sentinel.metrics.constants import METRICS_NAMESPACE


class SecretMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.version = Gauge(
            "version",
            "External secret bundle version loaded by Sentinel, or zero without a bundle.",
            namespace=METRICS_NAMESPACE,
            subsystem="secrets",
            registry=registry,
        )

    def set_version(self, version: int | None) -> None:
        self.version.set(version or 0)
