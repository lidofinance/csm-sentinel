import asyncio
import json
from datetime import time, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import ContractLogicError
import pytest
from web3.types import FilterParams, RPCEndpoint
from web3.utils.subscriptions import NewHeadsSubscription
from websockets.asyncio.server import serve

from sentinel.app.contracts import CommunityContractAddresses
from sentinel.app.health import HealthState
from sentinel.app.storage import BotStorage
from sentinel.chain import SharedChainConnection
from sentinel.config import Config, clear_config, set_config
from sentinel.module_types import ModuleType
from sentinel.modules.base import EventSource
from sentinel.rpc_provider import (
    FallbackAsyncWeb3,
    FallbackRequestProvider,
    FallbackSubscriptionProvider,
    RpcChainMismatch,
    RpcEndpointPool,
    RpcEndpointsUnavailable,
    RpcRequestRejectedByAllProviders,
    RpcSubscriptionReconnectRequired,
)
from sentinel.services.subscription import ModuleRuntimeSupervisor
from sentinel.web3_event_log_reader import Web3EventLogReader

from .helpers import start_anvil, start_anvil_node, stop_anvil


class _BlockOnlyAdapter:
    def __init__(self, addresses):
        self.addresses = addresses

    def event_sources(self):
        return (
            EventSource(
                name="test",
                address="0x0000000000000000000000000000000000000001",
                event_names=frozenset({"Ping"}),
            ),
        )

    def notifiable_events(self):
        return {"Ping"}

    def side_effect_events(self):
        return set()

    def topic_abis(self):
        return (
            [
                {
                    "anonymous": False,
                    "inputs": [],
                    "name": "Ping",
                    "type": "event",
                }
            ],
        )

    def event_aggregators(self):
        return ()

    def build_event_messages(self):
        return SimpleNamespace(get_notification_plan=None, event_handlers={})


class _Storage:
    def __init__(self):
        self.bot_data = {"block": 1}

    @property
    def state(self):
        return BotStorage(self.bot_data)

    def __call__(self):
        return self.state


class _Sink:
    async def emit(self, _notification):
        raise AssertionError("Block-only integration test must not emit notifications")


def _rpc_handler(
    *,
    error_methods: dict[str, int | tuple[int, str]] | None = None,
    method_results: dict[str, object] | None = None,
):
    errors = error_methods or {}
    results = method_results or {}

    async def handler(websocket):
        async for raw_request in websocket:
            request = json.loads(raw_request)
            method = request["method"]
            response = {"jsonrpc": "2.0", "id": request["id"]}
            if method in results:
                response["result"] = results[method]
            elif method in errors:
                configured_error = errors[method]
                if isinstance(configured_error, tuple):
                    code, message = configured_error
                else:
                    code, message = configured_error, "provider failure"
                response["error"] = {"code": code, "message": message}
            elif method == "eth_chainId":
                response["result"] = "0x1"
            elif method == "web3_clientVersion":
                response["result"] = "test-rpc"
            elif method == "eth_getLogs":
                response["result"] = []
            elif method == "eth_blockNumber":
                response["result"] = "0x2a"
            elif method == "eth_call":
                response["result"] = "0x"
            elif method == "eth_subscribe":
                response["result"] = "0x" + "11" * 32
            elif method == "eth_unsubscribe":
                response["result"] = True
            else:
                response["error"] = {"code": -32601, "message": "method not found"}
            await websocket.send(json.dumps(response))

    return handler


def _config(provider_urls: tuple[str, ...]) -> Config:
    addresses = CommunityContractAddresses(
        module="0x0000000000000000000000000000000000000001",
        accounting="0x0000000000000000000000000000000000000002",
        parameters_registry="0x0000000000000000000000000000000000000003",
        fee_distributor="0x0000000000000000000000000000000000000004",
        exit_penalties="0x0000000000000000000000000000000000000005",
        lido_locator="0x0000000000000000000000000000000000000006",
        staking_router="0x0000000000000000000000000000000000000007",
        vebo="0x0000000000000000000000000000000000000008",
        staking_module_id=1,
        module_type=ModuleType.COMMUNITY,
    )
    return Config(
        filestorage_path=".storage",
        token="token",
        web3_socket_providers=provider_urls,
        healthcheck_host="127.0.0.1",
        healthcheck_port=8080,
        contract_addresses=addresses,
        etherscan_url=None,
        beaconchain_url=None,
        module_ui_url=None,
        block_batch_size=10_000,
        process_blocks_requests_per_second=None,
        block_from=None,
        admin_ids=set(),
        deposit_digest_time=time(9, 0, tzinfo=timezone.utc),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pool_validation_rejects_mixed_chain_ids(unused_tcp_port_factory):
    mainnet = await start_anvil_node(unused_tcp_port_factory(), chain_id=1)
    hoodi = await start_anvil_node(unused_tcp_port_factory(), chain_id=560048)
    provider = FallbackRequestProvider(
        RpcEndpointPool((mainnet.ws_url, hoodi.ws_url)),
        role="validation",
        max_connection_retries=1,
    )

    try:
        with pytest.raises(RpcChainMismatch, match="chain ID 560048.*chain ID 1"):
            await provider.validate_endpoint_chain_ids()
    finally:
        await provider.disconnect()
        await asyncio.gather(stop_anvil(mainnet), stop_anvil(hoodi))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "error_code"),
    [
        ("eth_getLogs", -32000),
        ("eth_getLogs", -32600),
        ("eth_blockNumber", -32000),
        ("eth_call", -32005),
        ("eth_getLogs", 12345),
    ],
)
async def test_async_web3_requests_switch_provider_on_any_rpc_error(
    unused_tcp_port_factory,
    method,
    error_code,
):
    primary_port = unused_tcp_port_factory()
    fallback_port = unused_tcp_port_factory()
    primary_url = f"ws://127.0.0.1:{primary_port}"
    fallback_url = f"ws://127.0.0.1:{fallback_port}"
    pool = RpcEndpointPool((primary_url, fallback_url), cooldown_seconds=60)
    provider = FallbackRequestProvider(
        pool,
        role="backfill",
        max_connection_retries=1,
    )
    w3 = FallbackAsyncWeb3(provider)

    async with (
        serve(_rpc_handler(error_methods={method: error_code}), "127.0.0.1", primary_port),
        serve(_rpc_handler(), "127.0.0.1", fallback_port),
    ):
        try:
            if method == "eth_getLogs":
                await w3.eth.get_logs({"fromBlock": 1, "toBlock": 1})
            elif method == "eth_blockNumber":
                await w3.eth.get_block_number()
            else:
                await w3.eth.call({"to": "0x0000000000000000000000000000000000000001"})

            assert provider.active_endpoint == pool.endpoints[1]
            assert provider.connection_generation == 2
        finally:
            await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_rejected_endpoints_raise_one_infrastructure_error(
    unused_tcp_port_factory,
):
    primary_port = unused_tcp_port_factory()
    fallback_port = unused_tcp_port_factory()
    pool = RpcEndpointPool(
        (f"ws://127.0.0.1:{primary_port}", f"ws://127.0.0.1:{fallback_port}"),
        cooldown_seconds=0.2,
    )
    provider = FallbackRequestProvider(
        pool,
        role="backfill",
        max_connection_retries=1,
        retry_interval_seconds=0.01,
    )
    w3 = FallbackAsyncWeb3(provider)

    async with (
        serve(
            _rpc_handler(error_methods={"eth_getLogs": -32000}),
            "127.0.0.1",
            primary_port,
        ),
        serve(
            _rpc_handler(error_methods={"eth_getLogs": -32600}),
            "127.0.0.1",
            fallback_port,
        ),
    ):
        try:
            with pytest.raises(RpcRequestRejectedByAllProviders) as raised:
                await w3.eth.get_logs({"fromBlock": 1, "toBlock": 500})

            assert [failure.endpoint_index for failure in raised.value.failures] == [0, 1]
        finally:
            await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_request_failover_does_not_loop_on_unavailable_remaining_endpoint(
    unused_tcp_port_factory,
):
    primary_port = unused_tcp_port_factory()
    unavailable_fallback_port = unused_tcp_port_factory()
    pool = RpcEndpointPool(
        (
            f"ws://127.0.0.1:{primary_port}",
            f"ws://127.0.0.1:{unavailable_fallback_port}",
        ),
        cooldown_seconds=0.1,
    )
    provider = FallbackRequestProvider(
        pool,
        role="backfill",
        max_connection_retries=1,
        retry_interval_seconds=0.01,
    )
    w3 = FallbackAsyncWeb3(provider)

    async with serve(
        _rpc_handler(error_methods={"eth_getLogs": -32000}),
        "127.0.0.1",
        primary_port,
    ):
        try:
            with pytest.raises(RpcEndpointsUnavailable):
                await asyncio.wait_for(
                    w3.eth.get_logs({"fromBlock": 1, "toBlock": 1}),
                    timeout=2,
                )
        finally:
            await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_log_reader_shutdown_cancels_active_websocket_request(
    unused_tcp_port_factory,
):
    port = unused_tcp_port_factory()
    request_started = asyncio.Event()

    async def handler(websocket):
        async for raw_request in websocket:
            request = json.loads(raw_request)
            if request["method"] == "eth_getLogs":
                request_started.set()
                await websocket.wait_closed()
                return
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": "0x1",
            }
            await websocket.send(json.dumps(response))

    provider = FallbackRequestProvider(
        RpcEndpointPool((f"ws://127.0.0.1:{port}",)),
        role="backfill",
        max_connection_retries=1,
    )
    w3 = FallbackAsyncWeb3(provider)
    stop_event = asyncio.Event()
    reader = Web3EventLogReader(
        w3,
        request_interval_seconds=None,
        stop_event=stop_event,
    )

    async with serve(handler, "127.0.0.1", port):
        try:
            request_task = asyncio.create_task(
                reader.get_logs(
                    w3=w3,
                    filter_params=FilterParams(fromBlock=1, toBlock=1),
                )
            )
            await asyncio.wait_for(request_started.wait(), timeout=2)
            stop_event.set()

            assert await asyncio.wait_for(request_task, timeout=2) is None
        finally:
            await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_async_web3_does_not_switch_provider_on_contract_revert(
    unused_tcp_port_factory,
):
    primary_port = unused_tcp_port_factory()
    fallback_port = unused_tcp_port_factory()
    pool = RpcEndpointPool(
        (f"ws://127.0.0.1:{primary_port}", f"ws://127.0.0.1:{fallback_port}"),
        cooldown_seconds=60,
    )
    provider = FallbackRequestProvider(pool, role="reads", max_connection_retries=1)
    w3 = FallbackAsyncWeb3(provider)

    async with (
        serve(
            _rpc_handler(error_methods={"eth_call": (3, "execution reverted")}),
            "127.0.0.1",
            primary_port,
        ),
        serve(_rpc_handler(), "127.0.0.1", fallback_port),
    ):
        try:
            with pytest.raises(ContractLogicError, match="execution reverted"):
                await w3.eth.call({"to": "0x0000000000000000000000000000000000000001"})

            assert provider.active_endpoint == pool.endpoints[0]
            assert provider.connection_generation == 1
        finally:
            await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contract_call_preserves_formatters_after_provider_switch(
    unused_tcp_port_factory,
):
    primary_port = unused_tcp_port_factory()
    fallback_port = unused_tcp_port_factory()
    pool = RpcEndpointPool(
        (f"ws://127.0.0.1:{primary_port}", f"ws://127.0.0.1:{fallback_port}"),
        cooldown_seconds=60,
    )
    provider = FallbackRequestProvider(pool, role="reads", max_connection_retries=1)
    w3 = FallbackAsyncWeb3(provider)
    contract = w3.eth.contract(
        address="0x0000000000000000000000000000000000000001",
        abi=[
            {
                "inputs": [],
                "name": "value",
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }
        ],
    )

    async with (
        serve(
            _rpc_handler(error_methods={"eth_call": -32005}),
            "127.0.0.1",
            primary_port,
        ),
        serve(
            _rpc_handler(method_results={"eth_call": "0x" + f"{42:064x}"}),
            "127.0.0.1",
            fallback_port,
        ),
    ):
        try:
            assert await contract.functions.value().call() == 42
            assert provider.active_endpoint == pool.endpoints[1]
        finally:
            await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contract_call_survives_nested_chain_id_failover(
    unused_tcp_port_factory,
):
    primary_port = unused_tcp_port_factory()
    fallback_port = unused_tcp_port_factory()
    primary_chain_id_calls = 0

    async def primary_handler(websocket):
        nonlocal primary_chain_id_calls
        async for raw_request in websocket:
            request = json.loads(raw_request)
            response = {"jsonrpc": "2.0", "id": request["id"]}
            if request["method"] == "eth_chainId":
                primary_chain_id_calls += 1
                if primary_chain_id_calls == 1:
                    response["result"] = "0x1"
                else:
                    response["error"] = {"code": -32000, "message": "internal error"}
            else:
                response["error"] = {"code": -32601, "message": "method not found"}
            await websocket.send(json.dumps(response))

    expected_address = "0x0000000000000000000000000000000000000002"
    encoded_address = "0x" + "0" * 24 + expected_address[2:]
    pool = RpcEndpointPool(
        (f"ws://127.0.0.1:{primary_port}", f"ws://127.0.0.1:{fallback_port}"),
        cooldown_seconds=60,
    )
    provider = FallbackRequestProvider(pool, role="reads", max_connection_retries=1)
    w3 = FallbackAsyncWeb3(provider)
    contract = w3.eth.contract(
        address="0x0000000000000000000000000000000000000001",
        abi=[
            {
                "inputs": [],
                "name": "target",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            }
        ],
    )

    async with (
        serve(primary_handler, "127.0.0.1", primary_port),
        serve(
            _rpc_handler(method_results={"eth_call": encoded_address}),
            "127.0.0.1",
            fallback_port,
        ),
    ):
        try:
            assert await contract.functions.target().call() == w3.to_checksum_address(
                expected_address
            )
            assert provider.active_endpoint == pool.endpoints[1]
        finally:
            await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_serialized_read_flows_preserve_formatters_during_failover(
    unused_tcp_port_factory,
):
    primary_port = unused_tcp_port_factory()
    fallback_port = unused_tcp_port_factory()
    primary_chain_id_calls = 0

    async def primary_handler(websocket):
        nonlocal primary_chain_id_calls
        async for raw_request in websocket:
            request = json.loads(raw_request)
            response = {"jsonrpc": "2.0", "id": request["id"]}
            if request["method"] == "eth_chainId":
                primary_chain_id_calls += 1
                if primary_chain_id_calls == 1:
                    response["result"] = "0x1"
                else:
                    response["error"] = {"code": -32000, "message": "internal error"}
            elif request["method"] == "web3_clientVersion":
                response["result"] = "test-rpc"
            else:
                response["error"] = {"code": -32601, "message": "method not found"}
            await websocket.send(json.dumps(response))

    pool = RpcEndpointPool(
        (f"ws://127.0.0.1:{primary_port}", f"ws://127.0.0.1:{fallback_port}"),
        cooldown_seconds=60,
    )
    provider = FallbackRequestProvider(pool, role="reads", max_connection_retries=1)
    w3 = FallbackAsyncWeb3(provider)
    chain = SharedChainConnection(w3)
    contract = w3.eth.contract(
        address="0x0000000000000000000000000000000000000001",
        abi=[
            {
                "inputs": [],
                "name": "value",
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }
        ],
    )

    async def read_value() -> int:
        async with chain:
            return await contract.functions.value().call()

    async with (
        serve(primary_handler, "127.0.0.1", primary_port),
        serve(
            _rpc_handler(method_results={"eth_call": "0x" + f"{42:064x}"}),
            "127.0.0.1",
            fallback_port,
        ),
    ):
        try:
            assert await asyncio.gather(read_value(), read_value()) == [42, 42]
            assert primary_chain_id_calls == 2
        finally:
            await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscription_setup_failure_defers_reconnect_to_supervisor(
    unused_tcp_port_factory,
):
    primary_port = unused_tcp_port_factory()
    fallback_port = unused_tcp_port_factory()
    pool = RpcEndpointPool(
        (f"ws://127.0.0.1:{primary_port}", f"ws://127.0.0.1:{fallback_port}"),
        cooldown_seconds=60,
    )
    provider = FallbackSubscriptionProvider(pool, role="subscription", max_connection_retries=1)
    w3 = FallbackAsyncWeb3(provider)

    async with (
        serve(
            _rpc_handler(error_methods={"eth_subscribe": -32005}),
            "127.0.0.1",
            primary_port,
        ),
        serve(_rpc_handler(), "127.0.0.1", fallback_port),
    ):
        try:
            first_subscription = NewHeadsSubscription(handler=AsyncMock())
            with pytest.raises(RpcSubscriptionReconnectRequired, match="eth_subscribe"):
                await w3.subscription_manager.subscribe(first_subscription)

            assert provider.active_endpoint is None
            assert w3.subscription_manager.subscriptions == []

            await provider.connect()
            fallback_subscription = NewHeadsSubscription(handler=AsyncMock())
            subscription_id = await w3.subscription_manager.subscribe(fallback_subscription)

            assert subscription_id == "0x" + "11" * 32
            assert provider.active_endpoint == pool.endpoints[1]
            assert w3.subscription_manager.subscriptions == [fallback_subscription]
        finally:
            await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_reconnects_to_fallback_after_primary_anvil_stops(
    local_anvil_fork_launcher,
):
    source, launch_fork = local_anvil_fork_launcher
    source_w3 = AsyncWeb3(AsyncHTTPProvider(source.http_url))
    await source_w3.provider.make_request(RPCEndpoint("anvil_mine"), ["0x1"])
    await source_w3.provider.disconnect()

    primary = await asyncio.wait_for(launch_fork(1), timeout=30)
    fallback = await asyncio.wait_for(launch_fork(1), timeout=30)

    pool = RpcEndpointPool(
        (primary.ws_url, fallback.ws_url),
        cooldown_seconds=60,
    )
    provider = FallbackSubscriptionProvider(
        pool,
        role="integration",
        max_connection_rounds=1,
        retry_interval_seconds=0,
        max_connection_retries=2,
    )
    w3 = FallbackAsyncWeb3(provider)

    try:
        await asyncio.wait_for(provider.connect(), timeout=15)
        assert provider.active_endpoint == pool.endpoints[0]
        assert await asyncio.wait_for(w3.eth.block_number, timeout=10) == 1

        await stop_anvil(primary)
        await _wait_until_disconnected(provider)
        await provider.disconnect()
        await asyncio.wait_for(provider.connect(), timeout=15)

        assert provider.active_endpoint == pool.endpoints[1]
        assert provider.connection_generation == 2
        assert await asyncio.wait_for(w3.eth.block_number, timeout=10) == 1
    finally:
        await provider.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supervisor_replays_blocks_mined_during_primary_outage(
    local_anvil_fork_launcher,
):
    source, launch_fork = local_anvil_fork_launcher
    source_w3 = AsyncWeb3(AsyncHTTPProvider(source.http_url))
    await source_w3.provider.make_request(RPCEndpoint("anvil_mine"), ["0x1"])
    await source_w3.provider.disconnect()

    primary = await asyncio.wait_for(launch_fork(1), timeout=30)
    fallback = await asyncio.wait_for(launch_fork(1), timeout=30)
    pool = RpcEndpointPool((primary.ws_url, fallback.ws_url), cooldown_seconds=60)
    reads_provider = FallbackRequestProvider(pool, role="reads", max_connection_retries=2)
    backfill_provider = FallbackRequestProvider(pool, role="backfill", max_connection_retries=2)
    reads_w3 = FallbackAsyncWeb3(reads_provider)
    backfill_w3 = FallbackAsyncWeb3(backfill_provider)
    subscription_providers: list[FallbackSubscriptionProvider] = []

    def create_subscription_w3() -> FallbackAsyncWeb3:
        provider = FallbackSubscriptionProvider(
            pool,
            role="subscription",
            max_connection_retries=2,
        )
        subscription_providers.append(provider)
        return FallbackAsyncWeb3(provider)

    cfg = _config((primary.ws_url, fallback.ws_url))
    set_config(cfg)
    storage = _Storage()
    health = HealthState()
    chain = SharedChainConnection(reads_w3)
    supervisor = ModuleRuntimeSupervisor(
        create_subscription_w3,
        config=cfg,
        chain=chain,
        health=health,
        module_adapter=_BlockOnlyAdapter(cfg.contract_addresses),
        storage=storage,
        emit_notification=_Sink().emit,
        backfill_w3=backfill_w3,
    )
    original_runtime = supervisor.module_runtime
    supervisor_task = asyncio.create_task(supervisor.subscribe())

    try:
        await supervisor.wait_until_subscribed(timeout=10)
        assert await supervisor.establish_initial_checkpoint() == 0

        await stop_anvil(primary)
        fallback_w3 = AsyncWeb3(AsyncHTTPProvider(fallback.http_url))
        await fallback_w3.provider.make_request(RPCEndpoint("anvil_mine"), ["0x2"])
        await fallback_w3.provider.disconnect()

        await _wait_until(
            lambda: (
                supervisor.module_runtime is not original_runtime
                and storage.state.block.value == 3
                and not health.snapshot().catchup_active
            ),
            timeout_seconds=15,
        )

        assert len(subscription_providers) >= 2
        assert subscription_providers[-1].active_endpoint == pool.endpoints[1]
        assert storage.state.block.value == 3
    finally:
        supervisor.request_shutdown()
        await supervisor.raw_subscription.stop()
        await asyncio.wait_for(supervisor_task, timeout=10)
        await asyncio.gather(
            *(provider.disconnect() for provider in subscription_providers),
            reads_provider.disconnect(),
            backfill_provider.disconnect(),
            return_exceptions=True,
        )
        clear_config()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_retries_primary_before_using_fallback(local_anvil_fork_launcher):
    source, launch_fork = local_anvil_fork_launcher
    source_w3 = AsyncWeb3(AsyncHTTPProvider(source.http_url))
    await source_w3.provider.make_request(RPCEndpoint("anvil_mine"), ["0x1"])
    await source_w3.provider.disconnect()

    primary = await asyncio.wait_for(launch_fork(1), timeout=30)
    fallback = await asyncio.wait_for(launch_fork(1), timeout=30)
    primary_port = urlsplit(primary.ws_url).port
    assert primary_port is not None
    pool = RpcEndpointPool((primary.ws_url, fallback.ws_url), cooldown_seconds=60)
    provider = FallbackSubscriptionProvider(
        pool,
        role="integration",
        max_connection_retries=3,
    )
    restarted_primary = None

    try:
        await asyncio.wait_for(provider.connect(), timeout=10)
        assert provider.active_endpoint == pool.endpoints[0]

        await stop_anvil(primary)
        await _wait_until_disconnected(provider)
        await provider.disconnect()
        reconnect_task = asyncio.create_task(provider.connect())
        await asyncio.sleep(0.25)
        restarted_primary = await start_anvil(1, primary_port, source.http_url)
        await asyncio.wait_for(reconnect_task, timeout=10)

        assert provider.active_endpoint == pool.endpoints[0]
        assert provider.connection_generation == 2
    finally:
        await provider.disconnect()
        if restarted_primary is not None:
            await stop_anvil(restarted_primary)


async def _wait_until_disconnected(
    provider: FallbackSubscriptionProvider, timeout_seconds: float = 5.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while await provider.is_connected():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Primary Anvil websocket remained connected")
        await asyncio.sleep(0.05)


async def _wait_until(predicate, *, timeout_seconds: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Condition was not met before timeout")
