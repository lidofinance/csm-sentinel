from prometheus_client import REGISTRY, CollectorRegistry

from sentinel.metrics.rpc import RpcMetrics


class AppMetrics:
    """Application metrics grouped by subsystem in one registry."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.registry = registry
        self.rpc = RpcMetrics(registry)


DEFAULT_METRICS = AppMetrics()
