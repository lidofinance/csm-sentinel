from collections.abc import Mapping

from prometheus_client import CollectorRegistry, Gauge

from sentinel.app.build_info import load_build_info
from sentinel.metrics.constants import METRICS_NAMESPACE


class BuildInfoMetrics:
    def __init__(
        self,
        registry: CollectorRegistry,
        build_info: Mapping[str, str] | None = None,
    ) -> None:
        self.info = Gauge(
            "build_info",
            "Sentinel build information.",
            ("version", "branch", "commit"),
            namespace=METRICS_NAMESPACE,
            registry=registry,
        )
        labels = load_build_info() if build_info is None else build_info
        self.info.labels(
            version=labels["version"],
            branch=labels["branch"],
            commit=labels["commit"],
        ).set(1)
