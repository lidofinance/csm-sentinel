from prometheus_client import CollectorRegistry, generate_latest

from sentinel.metrics.secrets import SecretMetrics


def test_secret_version_metric_reports_loaded_bundle_version() -> None:
    registry = CollectorRegistry()
    metrics = SecretMetrics(registry)

    metrics.set_version(17)

    assert "sentinel_secrets_version 17.0" in generate_latest(registry).decode()
