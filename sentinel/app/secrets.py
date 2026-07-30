import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ENV_FILE_PATH_KEY = "ENV_FILE_PATH"
SECRETS_FILE_PATH_KEY = "SECRETS_FILE_PATH"
SECRET_VERSION_KEY = "SECRET_VERSION"
SECRET_WATCH_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class SecretBundle:
    path: Path
    version: int


def load_environment_files() -> SecretBundle | None:
    """Load the application and secret dotenv files with explicit precedence."""

    env_path = _configured_path(ENV_FILE_PATH_KEY)
    secret_path = _configured_path(SECRETS_FILE_PATH_KEY)
    for key, path in (
        (ENV_FILE_PATH_KEY, env_path),
        (SECRETS_FILE_PATH_KEY, secret_path),
    ):
        if path is not None and not path.is_file():
            raise RuntimeError(f"{key} does not point to a readable file: {path}")
    load_dotenv(env_path, override=False)
    if secret_path is None:
        return None

    load_dotenv(secret_path, override=True)
    return SecretBundle(secret_path, read_secret_version(secret_path))


def read_secret_version(path: Path) -> int:
    values = dotenv_values(path)
    return _parse_version(values.get(SECRET_VERSION_KEY), path)


def _configured_path(key: str) -> Path | None:
    value = os.getenv(key)
    return Path(value) if value else None


def _parse_version(raw_version: str | None, path: Path) -> int:
    if raw_version is None:
        raise RuntimeError(f"{SECRET_VERSION_KEY} is missing from {path}")
    version = int(raw_version)
    if version <= 0:
        raise RuntimeError(f"{SECRET_VERSION_KEY} must be positive in {path}")
    return version
