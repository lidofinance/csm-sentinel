from prometheus_client import CollectorRegistry

from sentinel.app.health import HealthState
from sentinel.metrics.chain import ChainMetrics


def _value(registry: CollectorRegistry, name: str, labels=None) -> float | None:
    return registry.get_sample_value(name, labels)


def test_chain_gauges_are_collected_from_bound_runtime_state():
    registry = CollectorRegistry()
    metrics = ChainMetrics(registry)
    health = HealthState()
    state = {"processed": 120}
    metrics.bind(
        health=health,
        processed_block=lambda: state["processed"],
    )
    health.mark_subscription_active()
    health.mark_catchup_started()

    assert _value(registry, "sentinel_chain_processed_block") == 120
    assert _value(registry, "sentinel_chain_subscription_active") == 1
    assert _value(registry, "sentinel_chain_catchup_active") == 1

    state["processed"] = 129
    health.mark_subscription_inactive()
    health.mark_catchup_complete()

    assert _value(registry, "sentinel_chain_processed_block") == 129
    assert _value(registry, "sentinel_chain_subscription_active") == 0
    assert _value(registry, "sentinel_chain_catchup_active") == 0


def test_subscription_recovery_counter_uses_bounded_reason():
    registry = CollectorRegistry()
    metrics = ChainMetrics(registry)

    metrics.subscription_recovered("rpc_disconnect")

    assert (
        _value(
            registry,
            "sentinel_chain_subscription_recoveries_total",
            {"reason": "rpc_disconnect"},
        )
        == 1
    )
