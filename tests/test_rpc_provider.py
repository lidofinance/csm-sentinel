import asyncio
from unittest.mock import AsyncMock

import pytest
from web3.exceptions import (
    BlockNotFound,
    ContractLogicError,
    PersistentConnectionClosedOK,
    ProviderConnectionError,
    TooManyRequests,
    Web3RPCError,
)

from sentinel.app.build_info import application_user_agent
from sentinel.rpc_provider import (
    DEFAULT_CONNECTION_RETRIES_PER_ENDPOINT,
    FallbackRequestProvider,
    FallbackSubscriptionProvider,
    RpcChainMismatch,
    RpcEndpointPool,
    RpcEndpointsUnavailable,
    RpcRequestRejectedByAllProviders,
    RpcSubscriptionReconnectRequired,
)


def _provider(pool: RpcEndpointPool, role: str = "test") -> FallbackSubscriptionProvider:
    return FallbackSubscriptionProvider(
        pool,
        role=role,
        max_connection_rounds=1,
        retry_interval_seconds=0,
    )


def _request_provider(pool: RpcEndpointPool, role: str = "test") -> FallbackRequestProvider:
    return FallbackRequestProvider(
        pool,
        role=role,
        max_connection_rounds=1,
        retry_interval_seconds=0,
    )


def test_provider_uses_bounded_retries_and_safe_endpoint_label():
    pool = RpcEndpointPool(("wss://user:token@rpc.example/ws/api-secret?key=secret",))
    provider = _provider(pool)

    assert provider._max_connection_retries == DEFAULT_CONNECTION_RETRIES_PER_ENDPOINT
    assert pool.endpoints[0].label == "rpc-1 (rpc.example)"
    assert pool.endpoints[0].metric_label == "rpc.example"


def test_rpc_providers_send_application_user_agent_in_websocket_handshake():
    pool = RpcEndpointPool(("wss://rpc.example",))
    request_provider = _request_provider(pool)
    subscription_provider = _provider(pool)

    assert request_provider._transports[0].websocket_kwargs["user_agent_header"] == (
        application_user_agent()
    )
    assert subscription_provider.websocket_kwargs["user_agent_header"] == (application_user_agent())


@pytest.mark.asyncio
async def test_provider_connects_to_fallback_when_primary_is_unavailable(monkeypatch):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    provider = _provider(pool)
    attempted: list[int] = []

    async def connect_endpoint(endpoint):
        attempted.append(endpoint.index)
        if endpoint.index == 0:
            raise ProviderConnectionError("primary unavailable")

    monkeypatch.setattr(provider, "_open_endpoint", connect_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", AsyncMock())
    monkeypatch.setattr(provider, "_read_chain_id", AsyncMock(return_value=1))

    await provider.connect()

    assert attempted == [0, 1]
    assert provider.active_endpoint == pool.endpoints[1]
    assert provider.connection_generation == 1


@pytest.mark.asyncio
async def test_provider_raises_when_endpoint_chain_differs_from_pool(monkeypatch):
    pool = RpcEndpointPool(
        ("ws://wrong-chain.invalid", "ws://correct-chain.invalid"),
        chain_id=560048,
    )
    provider = _request_provider(pool)
    attempted: list[int] = []

    async def connect_endpoint(endpoint):
        attempted.append(endpoint.index)

    monkeypatch.setattr(provider, "_open_endpoint", connect_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", AsyncMock())
    monkeypatch.setattr(provider, "_read_chain_id", AsyncMock(return_value=1))

    with pytest.raises(RpcChainMismatch, match="chain ID 1.*chain ID 560048"):
        await provider.validate_endpoint_chain_ids()

    assert attempted == [0]


@pytest.mark.asyncio
async def test_provider_validates_every_reachable_endpoint_chain(monkeypatch):
    pool = RpcEndpointPool(("ws://mainnet.invalid", "ws://hoodi.invalid"))
    provider = _request_provider(pool)
    attempted: list[int] = []

    async def connect_endpoint(endpoint):
        attempted.append(endpoint.index)

    monkeypatch.setattr(provider, "_open_endpoint", connect_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", AsyncMock())
    monkeypatch.setattr(provider, "_read_chain_id", AsyncMock(side_effect=[1, 560048]))

    with pytest.raises(RpcChainMismatch, match="chain ID 560048.*chain ID 1"):
        await provider.validate_endpoint_chain_ids()

    assert attempted == [0, 1]


@pytest.mark.asyncio
async def test_startup_validation_requires_every_endpoint(monkeypatch):
    pool = RpcEndpointPool(
        (
            "ws://primary.invalid",
            "ws://unavailable.invalid",
            "ws://fallback.invalid",
        )
    )
    provider = _request_provider(pool)
    attempted: list[int] = []

    async def connect_endpoint(endpoint):
        attempted.append(endpoint.index)
        if endpoint.index == 1:
            raise ProviderConnectionError("fallback unavailable")

    monkeypatch.setattr(provider, "_open_endpoint", connect_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", AsyncMock())
    monkeypatch.setattr(provider, "_read_chain_id", AsyncMock(return_value=1))

    with pytest.raises(RpcEndpointsUnavailable, match="rpc-2"):
        await provider.validate_endpoint_chain_ids()

    assert attempted == [0, 1, 2]


@pytest.mark.asyncio
async def test_too_many_requests_switches_endpoint_without_transport_retry(monkeypatch):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    provider = _request_provider(pool, role="reads")
    attempted: list[int] = []
    operation_calls = 0

    async def open_endpoint(endpoint):
        attempted.append(endpoint.index)

    async def close_endpoint():
        provider.active_endpoint = None

    async def is_connected():
        return provider.active_endpoint is not None

    async def operation():
        nonlocal operation_calls
        operation_calls += 1
        if operation_calls == 1:
            raise TooManyRequests("rate limited")
        return "ok"

    monkeypatch.setattr(provider, "is_connected", is_connected)
    monkeypatch.setattr(provider, "_open_endpoint", open_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", close_endpoint)

    assert await provider.execute_request(operation, method="eth_call") == "ok"
    assert attempted == [0, 1]
    assert operation_calls == 2


@pytest.mark.asyncio
async def test_repeated_subscription_transport_failure_cools_down_endpoint():
    pool = RpcEndpointPool(
        ("ws://primary.invalid", "ws://fallback.invalid"),
        cooldown_seconds=60,
    )
    primary = pool.endpoints[0]
    await pool.mark_success(primary)

    assert not await pool.record_subscription_transport_failure(primary)
    assert await pool.record_subscription_transport_failure(primary)

    candidates, _ = await pool.candidates()
    assert candidates == (pool.endpoints[1],)


@pytest.mark.asyncio
async def test_connect_does_not_recheck_validated_chain_id(monkeypatch):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    provider = _provider(pool)
    await pool.accept_chain_id(pool.endpoints[0], 1)
    await pool.accept_chain_id(pool.endpoints[1], 1)
    read_chain_id = AsyncMock()

    monkeypatch.setattr(provider, "_open_endpoint", AsyncMock())
    monkeypatch.setattr(provider, "_read_chain_id", read_chain_id)

    await provider.connect()

    read_chain_id.assert_not_awaited()
    assert provider.active_endpoint == pool.endpoints[0]


@pytest.mark.asyncio
async def test_shared_pool_keeps_the_successful_fallback_preferred(monkeypatch):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    subscription = _provider(pool, role="subscription")
    reads = _provider(pool, role="reads")
    subscription_attempts: list[int] = []
    reads_attempts: list[int] = []

    async def connect_subscription(endpoint):
        subscription_attempts.append(endpoint.index)
        if endpoint.index == 0:
            raise ProviderConnectionError("primary unavailable")

    async def connect_reads(endpoint):
        reads_attempts.append(endpoint.index)

    monkeypatch.setattr(subscription, "_open_endpoint", connect_subscription)
    monkeypatch.setattr(subscription, "_close_endpoint", AsyncMock())
    monkeypatch.setattr(subscription, "_read_chain_id", AsyncMock(return_value=1))
    monkeypatch.setattr(reads, "_open_endpoint", connect_reads)
    monkeypatch.setattr(reads, "_read_chain_id", AsyncMock(return_value=1))

    await subscription.connect()
    await reads.connect()

    assert subscription_attempts == [0, 1]
    assert reads_attempts == [1]


@pytest.mark.asyncio
async def test_provider_raises_after_all_endpoints_fail(monkeypatch):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    provider = _provider(pool)
    monkeypatch.setattr(
        provider,
        "_open_endpoint",
        AsyncMock(side_effect=ProviderConnectionError("unavailable")),
    )
    monkeypatch.setattr(provider, "_close_endpoint", AsyncMock())

    with pytest.raises(RpcEndpointsUnavailable, match="All RPC endpoints"):
        await provider.connect()


@pytest.mark.asyncio
async def test_execute_with_failover_retries_transport_failure_on_same_endpoint(monkeypatch):
    pool = RpcEndpointPool(
        ("ws://primary.invalid", "ws://fallback.invalid"),
        cooldown_seconds=60,
    )
    provider = _request_provider(pool)
    attempted: list[int] = []
    operation_calls = 0

    async def connect_endpoint(endpoint):
        attempted.append(endpoint.index)

    async def disconnect_endpoint():
        provider.active_endpoint = None

    async def is_connected():
        return provider.active_endpoint is not None

    async def operation():
        nonlocal operation_calls
        operation_calls += 1
        if operation_calls == 1:
            raise ProviderConnectionError("connection lost during discovery")
        return "ok"

    monkeypatch.setattr(provider, "is_connected", is_connected)
    monkeypatch.setattr(provider, "_open_endpoint", connect_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", disconnect_endpoint)
    monkeypatch.setattr(provider, "_read_chain_id", AsyncMock(return_value=1))

    assert await provider.execute_request(operation, method="eth_call") == "ok"
    assert attempted == [0, 0]
    assert operation_calls == 2


@pytest.mark.asyncio
async def test_execute_with_failover_switches_on_any_rpc_error(monkeypatch, caplog):
    pool = RpcEndpointPool(
        ("ws://primary.invalid", "ws://fallback.invalid"),
        cooldown_seconds=60,
    )
    provider = _request_provider(pool, role="backfill")
    provider.active_endpoint = pool.endpoints[0]
    await pool.mark_success(pool.endpoints[0])
    attempted: list[int] = []
    operation_calls = 0

    async def connect_endpoint(endpoint):
        attempted.append(endpoint.index)

    async def disconnect_endpoint():
        provider.active_endpoint = None

    async def is_connected():
        return provider.active_endpoint is not None

    async def operation():
        nonlocal operation_calls
        operation_calls += 1
        if operation_calls == 1:
            raise Web3RPCError(
                message="internal error",
                rpc_response={"error": {"code": -32000, "message": "internal error"}},
            )
        return []

    monkeypatch.setattr(provider, "is_connected", is_connected)
    monkeypatch.setattr(provider, "_open_endpoint", connect_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", disconnect_endpoint)
    monkeypatch.setattr(provider, "_read_chain_id", AsyncMock(return_value=1))

    assert (
        await provider.execute_request(
            operation,
            method="eth_getLogs",
        )
        == []
    )
    assert attempted == [1]
    assert operation_calls == 2
    assert provider.active_endpoint == pool.endpoints[1]
    assert "(RPC code -32000): internal error" in caplog.text


@pytest.mark.asyncio
async def test_rpc_error_log_redacts_url_secrets(monkeypatch, caplog):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    provider = _request_provider(pool, role="reads")

    async def close_endpoint():
        provider.active_endpoint = None

    async def is_connected():
        return provider.active_endpoint is not None

    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Web3RPCError(
                message="request failed",
                rpc_response={
                    "error": {
                        "code": -32000,
                        "message": (
                            "upstream wss://user:secret@rpc.example/private?token=secret failed"
                        ),
                    }
                },
            )
        return "ok"

    monkeypatch.setattr(provider, "is_connected", is_connected)
    monkeypatch.setattr(provider, "_open_endpoint", AsyncMock())
    monkeypatch.setattr(provider, "_close_endpoint", close_endpoint)

    assert await provider.execute_request(operation, method="eth_call") == "ok"
    assert "rpc.example" in caplog.text
    assert "user:secret" not in caplog.text
    assert "token=secret" not in caplog.text


@pytest.mark.asyncio
async def test_execute_with_failover_raises_after_every_endpoint_rejects(monkeypatch):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    provider = _request_provider(pool, role="reads")
    provider.active_endpoint = pool.endpoints[0]
    attempted: list[int] = []

    async def connect_endpoint(endpoint):
        attempted.append(endpoint.index)

    async def disconnect_endpoint():
        provider.active_endpoint = None

    async def is_connected():
        return provider.active_endpoint is not None

    async def operation():
        raise Web3RPCError(
            message="arbitrary RPC error",
            rpc_response={"error": {"code": 12345, "message": "arbitrary RPC error"}},
        )

    monkeypatch.setattr(provider, "is_connected", is_connected)
    monkeypatch.setattr(provider, "_open_endpoint", connect_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", disconnect_endpoint)
    monkeypatch.setattr(provider, "_read_chain_id", AsyncMock(return_value=1))

    with pytest.raises(RpcRequestRejectedByAllProviders) as raised:
        await provider.execute_request(operation, method="eth_getLogs")

    assert attempted == [1]
    assert [failure.endpoint_index for failure in raised.value.failures] == [0, 1]
    assert [failure.rpc_code for failure in raised.value.failures] == [12345, 12345]
    assert raised.value.__cause__ is None
    assert all(not hasattr(failure, "exception") for failure in raised.value.failures)


@pytest.mark.asyncio
async def test_execute_with_failover_does_not_wait_forever_for_remaining_endpoint(monkeypatch):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    provider = FallbackRequestProvider(
        pool,
        role="backfill",
        max_connection_rounds=-1,
        retry_interval_seconds=0,
    )
    provider.active_endpoint = pool.endpoints[0]
    attempted: list[int] = []

    async def connect_endpoint(endpoint):
        attempted.append(endpoint.index)
        raise ProviderConnectionError("fallback unavailable")

    async def disconnect_endpoint():
        provider.active_endpoint = None

    monkeypatch.setattr(provider, "is_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(provider, "_open_endpoint", connect_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", disconnect_endpoint)

    async def operation():
        raise Web3RPCError(
            message="primary rejected request",
            rpc_response={"error": {"code": -32000, "message": "internal error"}},
        )

    with pytest.raises(RpcEndpointsUnavailable):
        await provider.execute_request(operation, method="eth_getLogs")

    assert attempted == [1]


@pytest.mark.asyncio
async def test_execute_with_failover_propagates_semantic_web3_exception(monkeypatch):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    provider = _request_provider(pool, role="reads")
    provider.active_endpoint = pool.endpoints[0]
    disconnect = AsyncMock()

    monkeypatch.setattr(provider, "is_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(provider, "_close_endpoint", disconnect)

    async def operation():
        raise ContractLogicError("execution reverted")

    with pytest.raises(ContractLogicError, match="execution reverted"):
        await provider.execute_request(operation, method="eth_call")

    disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_with_failover_preserves_block_not_found(monkeypatch):
    pool = RpcEndpointPool(("ws://primary.invalid", "ws://fallback.invalid"))
    provider = _request_provider(pool, role="reads")
    provider.active_endpoint = pool.endpoints[0]
    disconnect = AsyncMock()
    error = BlockNotFound("missing block")

    monkeypatch.setattr(provider, "is_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(provider, "_close_endpoint", disconnect)

    async def operation():
        raise error

    with pytest.raises(BlockNotFound) as raised:
        await provider.execute_request(operation, method="eth_getBlockByNumber")

    assert raised.value is error
    disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscription_listener_normalizes_raw_rpc_error():
    provider = _provider(
        RpcEndpointPool(("ws://primary.invalid",)),
        role="subscription",
    )
    error = Web3RPCError(
        message="internal error",
        rpc_response={"error": {"code": -32000, "message": "internal error"}},
    )

    provider._provider_specific_socket_reader = AsyncMock(side_effect=error)  # noqa: SLF001

    with pytest.raises(RpcSubscriptionReconnectRequired) as raised:
        await provider._message_listener()  # noqa: SLF001

    assert raised.value.__cause__ is None
    assert raised.value.failure.rpc_code == -32000
    assert not hasattr(raised.value.failure, "exception")


@pytest.mark.asyncio
async def test_repeated_subscription_listener_failure_cools_down_endpoint(monkeypatch):
    pool = RpcEndpointPool(
        ("ws://primary.invalid", "ws://fallback.invalid"),
        cooldown_seconds=60,
    )
    provider = _provider(pool, role="subscription")
    primary = pool.endpoints[0]
    close_endpoint = AsyncMock()
    monkeypatch.setattr(provider, "_close_endpoint", close_endpoint)

    for expected_candidates in ((primary, pool.endpoints[1]), (pool.endpoints[1],)):
        provider.active_endpoint = None
        provider._listener_endpoint = primary  # noqa: SLF001
        await pool.mark_success(primary)

        provider._provider_specific_socket_reader = AsyncMock(  # noqa: SLF001
            side_effect=ProviderConnectionError("listener disconnected")
        )

        with pytest.raises(RpcSubscriptionReconnectRequired) as raised:
            await provider._message_listener()  # noqa: SLF001

        assert raised.value.failure.endpoint_index == primary.index
        candidates, _ = await pool.candidates()
        assert candidates == expected_candidates

    close_endpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_clean_subscription_listener_close_cools_down_endpoint():
    pool = RpcEndpointPool(
        ("ws://primary.invalid", "ws://fallback.invalid"),
        cooldown_seconds=60,
    )
    provider = _provider(pool, role="subscription")
    primary = pool.endpoints[0]

    for expected_candidates in ((primary, pool.endpoints[1]), (pool.endpoints[1],)):
        provider._listener_endpoint = primary  # noqa: SLF001
        await pool.mark_success(primary)
        provider._provider_specific_socket_reader = AsyncMock(  # noqa: SLF001
            side_effect=PersistentConnectionClosedOK(user_message="clean close")
        )

        await provider._message_listener()  # noqa: SLF001

        candidates, _ = await pool.candidates()
        assert candidates == expected_candidates


@pytest.mark.asyncio
async def test_subscription_listener_preserves_semantic_error():
    provider = _provider(
        RpcEndpointPool(("ws://primary.invalid",)),
        role="subscription",
    )
    error = BlockNotFound("missing block")

    provider._provider_specific_socket_reader = AsyncMock(side_effect=error)  # noqa: SLF001

    with pytest.raises(BlockNotFound) as raised:
        await provider._message_listener()  # noqa: SLF001

    assert raised.value is error


@pytest.mark.asyncio
async def test_concurrent_capacity_failures_do_not_disconnect_new_endpoint(monkeypatch):
    pool = RpcEndpointPool(
        ("ws://primary.invalid", "ws://fallback.invalid"),
        cooldown_seconds=60,
    )
    provider = _request_provider(pool, role="reads")
    provider.active_endpoint = pool.endpoints[0]
    provider.connection_generation = 1
    await pool.mark_success(pool.endpoints[0])
    attempted: list[int] = []
    operation_calls = 0
    both_primary_requests_started = asyncio.Event()

    async def connect_endpoint(endpoint):
        attempted.append(endpoint.index)

    async def disconnect_endpoint():
        provider.active_endpoint = None

    async def is_connected():
        return provider.active_endpoint is not None

    async def operation():
        nonlocal operation_calls
        operation_calls += 1
        if operation_calls <= 2:
            if operation_calls == 2:
                both_primary_requests_started.set()
            await both_primary_requests_started.wait()
            raise Web3RPCError(
                message="capacity exceeded",
                rpc_response={"error": {"code": -32005, "message": "capacity exceeded"}},
            )
        return "ok"

    monkeypatch.setattr(provider, "is_connected", is_connected)
    monkeypatch.setattr(provider, "_open_endpoint", connect_endpoint)
    monkeypatch.setattr(provider, "_close_endpoint", disconnect_endpoint)
    monkeypatch.setattr(provider, "_read_chain_id", AsyncMock(return_value=1))

    first, second = await asyncio.gather(
        provider.execute_request(operation, method="eth_getLogs"),
        provider.execute_request(operation, method="eth_getLogs"),
    )

    assert first == second == "ok"
    assert attempted == [1]
    assert provider.active_endpoint == pool.endpoints[1]
