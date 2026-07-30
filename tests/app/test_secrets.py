import os
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.app.secrets import (
    SecretBundle,
    load_environment_files,
)


def test_environment_files_load_in_precedence_order(tmp_path: Path) -> None:
    environment = tmp_path / "app.env"
    secrets = tmp_path / "secrets.env"
    environment.write_text("MODULE_ADDRESS=runtime\nTOKEN=runtime\n")
    secrets.write_text(
        'SECRET_VERSION=7\nTOKEN="secret$token"\nWEB3_SOCKET_PROVIDERS="wss://rpc"\n'
    )

    with patch.dict(
        os.environ,
        {
            "ENV_FILE_PATH": str(environment),
            "SECRETS_FILE_PATH": str(secrets),
        },
        clear=True,
    ):
        bundle = load_environment_files()

        assert bundle == SecretBundle(secrets, 7)
        assert os.environ["MODULE_ADDRESS"] == "runtime"
        assert os.environ["TOKEN"] == "secret$token"
        assert os.environ["WEB3_SOCKET_PROVIDERS"] == "wss://rpc"
        assert os.environ["SECRET_VERSION"] == "7"


def test_environment_files_preserve_process_env_for_non_secrets(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.env"
    runtime.write_text("MODULE_ADDRESS=runtime\n")

    with patch.dict(
        os.environ,
        {
            "ENV_FILE_PATH": str(runtime),
            "MODULE_ADDRESS": "process",
        },
        clear=True,
    ):
        bundle = load_environment_files()

        assert bundle is None
        assert os.environ["MODULE_ADDRESS"] == "process"


def test_environment_file_paths_can_be_configured(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.env"
    secrets = tmp_path / "secrets.env"
    runtime.write_text("MODULE_ADDRESS=runtime\n")
    secrets.write_text("TOKEN=secret\nSECRET_VERSION=3\n")

    with patch.dict(
        os.environ,
        {
            "ENV_FILE_PATH": str(runtime),
            "SECRETS_FILE_PATH": str(secrets),
        },
        clear=True,
    ):
        bundle = load_environment_files()

        assert bundle == SecretBundle(secrets, 3)
        assert os.environ["MODULE_ADDRESS"] == "runtime"
        assert os.environ["TOKEN"] == "secret"


def test_secret_bundle_requires_version(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("TOKEN=secret\n")

    with (
        patch.dict(
            os.environ,
            {"SECRETS_FILE_PATH": str(secrets)},
            clear=True,
        ),
        pytest.raises(RuntimeError, match="SECRET_VERSION is missing"),
    ):
        load_environment_files()


def test_configured_environment_file_must_exist(tmp_path: Path) -> None:
    with (
        patch.dict(
            os.environ,
            {"SECRETS_FILE_PATH": str(tmp_path / "missing")},
            clear=True,
        ),
        pytest.raises(RuntimeError, match="SECRETS_FILE_PATH does not point"),
    ):
        load_environment_files()
