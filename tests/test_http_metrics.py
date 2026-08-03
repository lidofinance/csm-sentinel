from types import SimpleNamespace
from typing import cast

import aiohttp
import pytest
from prometheus_client import CollectorRegistry, generate_latest
from yarl import URL

from sentinel.metrics.http import HttpMetrics, HttpMetricsMiddleware


def _request() -> aiohttp.ClientRequest:
    return cast(
        aiohttp.ClientRequest,
        SimpleNamespace(method="get", url=URL("https://ipfs.io/ipfs/cid")),
    )


@pytest.mark.asyncio
async def test_http_middleware_records_status_without_path_label() -> None:
    registry = CollectorRegistry()
    middleware = HttpMetricsMiddleware(HttpMetrics(registry))
    response = cast(aiohttp.ClientResponse, SimpleNamespace(status=200))

    async def handler(_request: aiohttp.ClientRequest) -> aiohttp.ClientResponse:
        return response

    assert await middleware(_request(), handler) is response

    payload = generate_latest(registry).decode()
    assert 'sentinel_http_requests_total{host="ipfs.io",method="GET",outcome="2xx"} 1.0' in payload
    assert "/ipfs/cid" not in payload


@pytest.mark.asyncio
async def test_http_middleware_records_transport_error() -> None:
    registry = CollectorRegistry()
    middleware = HttpMetricsMiddleware(HttpMetrics(registry))

    async def handler(_request: aiohttp.ClientRequest) -> aiohttp.ClientResponse:
        raise aiohttp.ClientConnectionError("unavailable")

    with pytest.raises(aiohttp.ClientConnectionError):
        await middleware(_request(), handler)

    payload = generate_latest(registry).decode()
    assert (
        'sentinel_http_requests_total{host="ipfs.io",method="GET",outcome="transport_error"} 1.0'
        in payload
    )
