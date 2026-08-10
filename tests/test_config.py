import subprocess
import sys
from datetime import UTC, time

import pytest

from sentinel.config import get_healthcheck_bind_from_env, load_config_from_env


def test_config_can_be_imported_cold():
    result = subprocess.run(
        [sys.executable, "-c", "import sentinel.config"],
        cwd="/Users/skhomuti/csm-bot/csm-watcher",
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_load_config_accepts_legacy_provider_env(monkeypatch):
    monkeypatch.delenv("WEB3_SOCKET_PROVIDERS", raising=False)
    monkeypatch.setenv(
        "WEB3_SOCKET_PROVIDER",
        "wss://legacy-primary.invalid/ws,wss://legacy-fallback.invalid/ws",
    )
    monkeypatch.setenv("MODULE_ADDRESS", "0x0000000000000000000000000000000000000001")

    cfg = load_config_from_env()

    assert cfg.web3_socket_providers == (
        "wss://legacy-primary.invalid/ws",
        "wss://legacy-fallback.invalid/ws",
    )


def test_load_config_prefers_plural_provider_env(monkeypatch):
    monkeypatch.setenv("WEB3_SOCKET_PROVIDERS", "wss://plural.invalid/ws")
    monkeypatch.setenv("WEB3_SOCKET_PROVIDER", "wss://legacy.invalid/ws")
    monkeypatch.setenv("MODULE_ADDRESS", "0x0000000000000000000000000000000000000001")

    cfg = load_config_from_env()

    assert cfg.web3_socket_providers == ("wss://plural.invalid/ws",)


def test_load_config_reads_module_envs(monkeypatch):
    monkeypatch.setenv("WEB3_SOCKET_PROVIDERS", "wss://example.invalid/ws")
    monkeypatch.setenv("MODULE_ADDRESS", "0x0000000000000000000000000000000000000001")
    monkeypatch.setenv("MODULE_UI_URL", "https://module.example")

    cfg = load_config_from_env()

    assert cfg.module_address == "0x0000000000000000000000000000000000000001"
    assert cfg.module_ui_url == "https://module.example"


def test_load_config_reads_deposit_digest_time_as_utc(monkeypatch):
    monkeypatch.setenv("WEB3_SOCKET_PROVIDERS", "wss://example.invalid/ws")
    monkeypatch.setenv("MODULE_ADDRESS", "0x0000000000000000000000000000000000000001")
    monkeypatch.setenv("DEPOSIT_DIGEST_TIME", "17:45")

    cfg = load_config_from_env()

    assert cfg.deposit_digest_time == time(17, 45, tzinfo=UTC)


@pytest.mark.parametrize("value", ["17:45:01", "17:45+05:00", "midday"])
def test_load_config_rejects_invalid_deposit_digest_time(monkeypatch, value):
    monkeypatch.setenv("WEB3_SOCKET_PROVIDERS", "wss://example.invalid/ws")
    monkeypatch.setenv("MODULE_ADDRESS", "0x0000000000000000000000000000000000000001")
    monkeypatch.setenv("DEPOSIT_DIGEST_TIME", value)

    with pytest.raises(RuntimeError, match="DEPOSIT_DIGEST_TIME"):
        load_config_from_env()


def test_load_config_leaves_module_ui_unset(monkeypatch):
    monkeypatch.setenv("WEB3_SOCKET_PROVIDERS", "wss://example.invalid/ws")
    monkeypatch.setenv("MODULE_ADDRESS", "0x0000000000000000000000000000000000000001")
    monkeypatch.delenv("MODULE_UI_URL", raising=False)

    cfg = load_config_from_env()

    assert cfg.module_ui_url is None


def test_load_config_reads_healthcheck_envs(monkeypatch):
    monkeypatch.setenv("WEB3_SOCKET_PROVIDERS", "wss://example.invalid/ws")
    monkeypatch.setenv("MODULE_ADDRESS", "0x0000000000000000000000000000000000000001")
    monkeypatch.setenv("HEALTHCHECK_HOST", "127.0.0.1")
    monkeypatch.setenv("HEALTHCHECK_PORT", "18080")

    cfg = load_config_from_env()

    assert cfg.healthcheck_host == "127.0.0.1"
    assert cfg.healthcheck_port == 18080


def test_get_healthcheck_bind_from_env_rejects_invalid_port(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PORT", "70000")

    with pytest.raises(RuntimeError, match="HEALTHCHECK_PORT"):
        get_healthcheck_bind_from_env()
