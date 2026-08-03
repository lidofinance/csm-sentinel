from unittest.mock import AsyncMock, patch

import pytest
from prometheus_client import CollectorRegistry

from sentinel.metrics.telegram import MetricsHTTPXRequest, TelegramMetrics


def _value(registry: CollectorRegistry, name: str, labels: dict[str, str]) -> float | None:
    return registry.get_sample_value(name, labels)


@pytest.mark.asyncio
async def test_request_middleware_records_bot_api_http_attempt():
    registry = CollectorRegistry()
    request = MetricsHTTPXRequest(TelegramMetrics(registry))

    with patch(
        "telegram.request.HTTPXRequest.do_request",
        new=AsyncMock(return_value=(200, b"{}")),
    ):
        await request.do_request(
            "https://api.telegram.org/bot-secret/sendMessage",
            "POST",
        )

    assert (
        _value(
            registry,
            "sentinel_telegram_requests_total",
            {"method": "sendMessage", "outcome": "2xx"},
        )
        == 1
    )


@pytest.mark.asyncio
async def test_request_middleware_normalizes_file_path_and_transport_error():
    registry = CollectorRegistry()
    request = MetricsHTTPXRequest(TelegramMetrics(registry))

    with (
        patch(
            "telegram.request.HTTPXRequest.do_request",
            new=AsyncMock(side_effect=OSError("contains secret")),
        ),
        pytest.raises(OSError, match="contains secret"),
    ):
        await request.do_request(
            "https://api.telegram.org/file/bot-secret/documents/private-name.pdf",
            "GET",
        )

    assert (
        _value(
            registry,
            "sentinel_telegram_requests_total",
            {"method": "file_download", "outcome": "transport_error"},
        )
        == 1
    )
