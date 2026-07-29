from prometheus_client import REGISTRY, CollectorRegistry

from sentinel.metrics.jobs import JobMetrics
from sentinel.metrics.rpc import RpcMetrics
from sentinel.metrics.telegram import TelegramMetrics


class AppMetrics:
    """Application metrics grouped by subsystem in one registry."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.registry = registry
        self.jobs = JobMetrics(registry)
        self.rpc = RpcMetrics(registry)
        self.telegram = TelegramMetrics(registry)


DEFAULT_METRICS = AppMetrics()
