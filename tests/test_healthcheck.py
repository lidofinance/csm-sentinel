import pytest

from scripts import healthcheck


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


def test_check_health_uses_configured_port(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PORT", "18080")
    calls = []

    def fake_urlopen(url, *, timeout):
        calls.append((url, timeout))
        return _Response(200)

    monkeypatch.setattr(healthcheck, "urlopen", fake_urlopen)

    healthcheck.check_health()

    assert calls == [("http://127.0.0.1:18080/live", 3.0)]


def test_check_health_rejects_non_success_response(monkeypatch):
    monkeypatch.setattr(healthcheck, "urlopen", lambda *args, **kwargs: _Response(503))

    with pytest.raises(RuntimeError, match="HTTP 503"):
        healthcheck.check_health()


def test_check_health_rejects_invalid_port(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PORT", "not-a-port")

    with pytest.raises(ValueError, match="must be an integer"):
        healthcheck.check_health()
