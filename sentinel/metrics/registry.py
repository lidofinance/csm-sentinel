from prometheus_client import REGISTRY, CollectorRegistry

from sentinel.metrics.aggregation import AggregationMetrics
from sentinel.metrics.build import BuildInfoMetrics
from sentinel.metrics.chain import ChainMetrics
from sentinel.metrics.http import HttpMetrics
from sentinel.metrics.jobs import JobMetrics
from sentinel.metrics.rpc import RpcMetrics
from sentinel.metrics.secrets import SecretMetrics
from sentinel.metrics.telegram import TelegramMetrics


class AppMetrics:
    """Application metrics grouped by subsystem in one registry."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.registry = registry
        self.aggregation = AggregationMetrics(registry)
        self.build = BuildInfoMetrics(registry)
        self.chain = ChainMetrics(registry)
        self.http = HttpMetrics(registry)
        self.jobs = JobMetrics(registry)
        self.rpc = RpcMetrics(registry)
        self.secrets = SecretMetrics(registry)
        self.telegram = TelegramMetrics(registry)


DEFAULT_METRICS = AppMetrics()
