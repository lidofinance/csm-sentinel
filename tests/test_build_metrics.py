from prometheus_client import CollectorRegistry, generate_latest

from sentinel.metrics.build import BuildInfoMetrics


def test_build_info_metric_exposes_build_labels() -> None:
    registry = CollectorRegistry()

    BuildInfoMetrics(
        registry,
        {
            "version": "1.2.3",
            "branch": "release/1.2",
            "commit": "deadbeef",
        },
    )

    payload = generate_latest(registry).decode()
    assert (
        'sentinel_build_info{branch="release/1.2",commit="deadbeef",version="1.2.3"} 1.0' in payload
    )
