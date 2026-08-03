from prometheus_client import CollectorRegistry, generate_latest

from sentinel.app.build_info import application_user_agent
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


def test_application_user_agent_contains_name_and_version() -> None:
    assert application_user_agent({"version": "1.2.3"}) == "sm-sentinel/1.2.3"
