import json
from urllib.error import HTTPError
from urllib.request import urlopen

from sentinel.app.health import (
    HealthServer,
    HealthState,
    LIVENESS_STALE_AFTER_SECONDS,
    READINESS_PROGRESS_STALE_AFTER_SECONDS,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_health_state_transitions():
    clock = FakeClock()
    health = HealthState(clock=clock)

    snapshot = health.snapshot()
    assert snapshot.startup_complete is False
    assert snapshot.ready is False
    assert snapshot.live is True

    health.mark_polling_started()
    health.mark_subscription_active()
    health.mark_warmup_started()
    health.mark_warmup_complete()
    health.mark_startup_complete()

    snapshot = health.snapshot()
    assert snapshot.startup_complete is True
    assert snapshot.ready is True
    assert snapshot.live is True
    assert snapshot.warmup_started is True
    assert snapshot.warmup_complete is True
    assert snapshot.warmup_error is None

    health.mark_subscription_inactive()
    snapshot = health.snapshot()
    assert snapshot.ready is False
    assert snapshot.live is True

    health.mark_fatal_error("boom")
    snapshot = health.snapshot()
    assert snapshot.live is False
    assert snapshot.ready is False


def test_health_state_warmup_failure_is_reported_without_breaking_readiness():
    clock = FakeClock()
    health = HealthState(clock=clock)
    health.mark_polling_started()
    health.mark_subscription_active()
    health.mark_warmup_started()
    health.mark_warmup_failed("cache unavailable")
    health.mark_startup_complete()

    snapshot = health.snapshot()

    assert snapshot.ready is True
    assert snapshot.live is True
    assert snapshot.warmup_started is True
    assert snapshot.warmup_complete is True
    assert snapshot.warmup_error == "cache unavailable"


def test_health_state_stale_progress_affects_readiness_only():
    clock = FakeClock()
    health = HealthState(clock=clock)
    health.mark_polling_started()
    health.mark_subscription_active()
    health.mark_startup_complete()

    clock.advance(READINESS_PROGRESS_STALE_AFTER_SECONDS + 1)
    health.heartbeat()
    snapshot = health.snapshot()

    assert snapshot.live is True
    assert snapshot.ready is False


def test_health_state_stale_heartbeat_breaks_liveness():
    clock = FakeClock()
    health = HealthState(clock=clock)
    clock.advance(LIVENESS_STALE_AFTER_SECONDS + 1)

    snapshot = health.snapshot()

    assert snapshot.live is False
    assert snapshot.ready is False


def test_health_state_is_not_ready_during_catchup():
    health = HealthState()
    health.mark_polling_started()
    health.mark_subscription_active()
    health.mark_startup_complete()

    health.mark_catchup_started()
    assert health.snapshot().ready is False
    assert health.snapshot().catchup_active is True

    health.mark_catchup_complete()
    assert health.snapshot().ready is True
    assert health.snapshot().catchup_active is False


def test_health_server_reports_status_endpoints():
    health = HealthState()
    build_info = {"version": "v1.2.3", "branch": "main", "commit": "abc123"}
    server = HealthServer(health, host="127.0.0.1", port=0, build_info=build_info)
    server.start()
    try:
        base_url = f"http://127.0.0.1:{server.port}"

        try:
            urlopen(f"{base_url}/startup", timeout=1)
            raise AssertionError("startup should fail before initialization")
        except HTTPError as exc:
            assert exc.code == 503

        with urlopen(f"{base_url}/build-info.json", timeout=1) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert json.loads(response.read()) == build_info

        health.mark_polling_started()
        health.mark_subscription_active()
        health.mark_warmup_started()
        health.mark_warmup_complete()
        health.mark_startup_complete()

        with urlopen(f"{base_url}/ready", timeout=1) as response:
            assert response.status == 200
            payload = json.loads(response.read())
            assert payload["warmup_started"] is True
            assert payload["warmup_complete"] is True
            assert payload["warmup_error"] is None

        health.mark_fatal_error("fatal")
        try:
            urlopen(f"{base_url}/live", timeout=1)
            raise AssertionError("liveness should fail after fatal error")
        except HTTPError as exc:
            assert exc.code == 503
    finally:
        server.stop()
