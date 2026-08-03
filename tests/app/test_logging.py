import json
import logging
from io import StringIO

from sentinel.app.logging import JsonFormatter, SecretRedactor


def _render(
    message: object,
    args: tuple[object, ...] = (),
    *,
    exc_info=None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("sentinel.test.logging")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info(message, *args, exc_info=exc_info, extra=extra)
    return json.loads(stream.getvalue())


def test_json_formatter_preserves_lazy_log_arguments() -> None:
    payload = _render("Processed block %s", (42,), extra={"event": "block_processed"})

    assert payload["level"] == "INFO"
    assert payload["logger"] == "sentinel.test.logging"
    assert payload["message"] == "Processed block 42"
    assert payload["event"] == "block_processed"
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_redacts_common_secret_shapes() -> None:
    payload = _render(
        "RPC wss://user:password@rpc.example/ws?api_key=value failed; "
        "Authorization: Bearer bearer-token"
    )

    message = payload["message"]
    assert "password" not in message
    assert "api_key=value" not in message
    assert "bearer-token" not in message
    assert "rpc.example" in message


def test_json_formatter_redacts_nested_sensitive_fields() -> None:
    payload = _render(
        "Request failed",
        extra={
            "context": {
                "token": "telegram-token",
                "nested": [{"password": "password-value"}],
                "endpoint": "rpc.example",
            }
        },
    )

    assert payload["context"] == {
        "token": "***",
        "nested": [{"password": "***"}],
        "endpoint": "rpc.example",
    }


def test_json_formatter_redacts_registered_value_from_exception() -> None:
    from sentinel.app.logging import REDACTOR

    REDACTOR.register("actual-secret-value")
    try:
        raise RuntimeError("request with actual-secret-value failed")
    except RuntimeError:
        payload = _render("Request failed", exc_info=True)

    assert "actual-secret-value" not in payload["exception"]
    assert "***" in payload["exception"]


def test_secret_redactor_masks_sensitive_mapping_keys() -> None:
    redactor = SecretRedactor()

    assert redactor.redact({"api-key": "value", "safe": "visible"}) == {
        "api-key": "***",
        "safe": "visible",
    }


def test_secret_redactor_masks_suffixed_sensitive_mapping_keys() -> None:
    redactor = SecretRedactor()

    assert redactor.redact({"telegram_bot_token": "value"}) == {"telegram_bot_token": "***"}


def test_secret_redactor_does_not_render_unknown_objects() -> None:
    class SensitiveObject:
        def __str__(self) -> str:
            raise AssertionError("unknown objects must not be rendered")

    redactor = SecretRedactor()

    assert redactor.redact(SensitiveObject()) == "<SensitiveObject>"
