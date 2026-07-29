import os
from urllib.request import urlopen

DEFAULT_HEALTHCHECK_PORT = 8080
HEALTHCHECK_TIMEOUT_SECONDS = 3.0


def _healthcheck_port() -> int:
    raw_port = os.getenv("HEALTHCHECK_PORT", str(DEFAULT_HEALTHCHECK_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("HEALTHCHECK_PORT must be an integer") from exc

    if not 1 <= port <= 65535:
        raise ValueError("HEALTHCHECK_PORT must be between 1 and 65535")
    return port


def check_health() -> None:
    port = _healthcheck_port()
    url = f"http://127.0.0.1:{port}/live"

    try:
        with urlopen(url, timeout=HEALTHCHECK_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RuntimeError(f"Health endpoint returned HTTP {response.status}")
    except OSError as exc:
        raise RuntimeError(f"Healthcheck request failed: {exc}") from exc


if __name__ == "__main__":
    try:
        check_health()
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
