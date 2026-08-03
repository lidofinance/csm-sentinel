import json
import logging
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

REDACTED: str = "***"

_URL_USERINFO_PATTERN = re.compile(
    r"(?P<scheme>\b(?:https?|wss?)://)[^/@\s]+@",
    re.IGNORECASE,
)
_QUERY_SECRET_PATTERN = re.compile(
    r"(?P<prefix>[?&](?:password|passwd|secret|token|access[_-]?token|"
    r"refresh[_-]?token|api[_-]?key|key)=)[^&#\s]+",
    re.IGNORECASE,
)
_NAMED_SECRET_PATTERN = re.compile(
    r"(?P<prefix>\b(?:password|passwd|secret|token|access[_-]?token|"
    r"refresh[_-]?token|api[_-]?key|authorization)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&}]+)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(
    r"(?P<prefix>\bAuthorization\s*:\s*Bearer\s+)[A-Za-z0-9._~+\-/]+=*",
    re.IGNORECASE,
)
_TELEGRAM_TOKEN_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")

_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
    }
)


class SecretRedactor:
    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def register(self, *values: str | None) -> None:
        with self._lock:
            self._values.update(value for value in values if value)

    def redact(self, value: Any, *, key: str | None = None) -> Any:
        if key is not None and _is_sensitive_key(key):
            return REDACTED
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, Mapping):
            return {
                str(item_key): self.redact(item_value, key=str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self.redact(item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return f"<{type(value).__name__}>"

    def _redact_text(self, value: str) -> str:
        with self._lock:
            sensitive_values = sorted(self._values, key=len, reverse=True)
        for sensitive_value in sensitive_values:
            value = str(value).replace(str(sensitive_value), str(REDACTED))
        value = _URL_USERINFO_PATTERN.sub(r"\g<scheme>***@", value)
        value = _QUERY_SECRET_PATTERN.sub(r"\g<prefix>***", value)
        value = _BEARER_PATTERN.sub(r"\g<prefix>***", value)
        value = _NAMED_SECRET_PATTERN.sub(r"\g<prefix>***", value)
        return _TELEGRAM_TOKEN_PATTERN.sub(REDACTED, value)


REDACTOR = SecretRedactor()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_")
            }
        )
        return json.dumps(
            REDACTOR.redact(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def register_sensitive_values(*values: str | None) -> None:
    REDACTOR.register(*values)


def register_sensitive_environment() -> None:
    REDACTOR.register(*(value for key, value in os.environ.items() if _is_sensitive_key(key)))


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEY_NAMES or any(
        normalized.endswith(f"_{suffix}") for suffix in _SENSITIVE_KEY_NAMES
    )
