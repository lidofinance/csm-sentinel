"""Utilities supporting the integration tests."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from hexbytes import HexBytes
from web3 import AsyncHTTPProvider, AsyncWeb3, WebSocketProvider
from web3.exceptions import TransactionNotFound
from web3.types import RPCEndpoint, TxParams, TxReceipt

from sentinel.app.storage import BotStorage
from sentinel.models import EventNotification
from sentinel.app.health import HealthState
from sentinel.module_types import ModuleType
from sentinel.services.subscription import ModuleRuntimeSupervisor, build_module_runtime


@dataclass
class AnvilInstance:
    process: asyncio.subprocess.Process
    http_url: str
    ws_url: str


async def wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for {host}:{port}")
            await asyncio.sleep(0.1)
            continue
        writer.close()
        await writer.wait_closed()
        return


def _normalise_fork_url(fork_url: str) -> str:
    if fork_url.startswith("ws://"):
        return "http://" + fork_url[len("ws://") :]
    if fork_url.startswith("wss://"):
        return "https://" + fork_url[len("wss://") :]
    return fork_url


async def start_anvil(fork_block: int, port: int, fork_url: str) -> AnvilInstance:
    if not fork_url:
        raise RuntimeError("WEB3_SOCKET_PROVIDERS must be configured for integration tests")

    fork_source = _normalise_fork_url(fork_url)
    cmd = [
        "anvil",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--fork-url",
        fork_source,
        "--fork-block-number",
        str(fork_block),
    ]
    process = await asyncio.create_subprocess_exec(*cmd)
    try:
        await wait_for_port("127.0.0.1", port)
    except Exception:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise
    return AnvilInstance(
        process=process, http_url=f"http://127.0.0.1:{port}", ws_url=f"ws://127.0.0.1:{port}"
    )


async def start_anvil_node(port: int, *, chain_id: int | None = None) -> AnvilInstance:
    cmd = [
        "anvil",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if chain_id is not None:
        cmd.extend(("--chain-id", str(chain_id)))
    process = await asyncio.create_subprocess_exec(*cmd)
    try:
        await wait_for_port("127.0.0.1", port)
    except Exception:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise
    return AnvilInstance(
        process=process,
        http_url=f"http://127.0.0.1:{port}",
        ws_url=f"ws://127.0.0.1:{port}",
    )


async def stop_anvil(instance: AnvilInstance) -> None:
    if instance.process.returncode is not None:
        return
    instance.process.terminate()
    try:
        await asyncio.wait_for(instance.process.wait(), timeout=5.0)
    except asyncio.TimeoutError:  # pragma: no cover - defensive cleanup
        instance.process.kill()
        await instance.process.wait()


async def build_subscription(ws_url: str, http_url: str) -> "EventReplayHarness":
    from sentinel.config import get_config, set_config
    from sentinel.app.module_adapter import build_module_adapter_from_config
    from sentinel.chain import SharedChainConnection

    persistent_w3 = AsyncWeb3(WebSocketProvider(ws_url, max_connection_retries=-1))
    backfill_w3 = AsyncWeb3(AsyncHTTPProvider(http_url))
    w3 = AsyncWeb3(WebSocketProvider(ws_url, max_connection_retries=-1))
    cfg = get_config()
    try:
        addresses = await discover_contract_addresses_from_url(
            http_url, cfg.contract_addresses.module
        )
    except Exception:
        if cfg.contract_addresses.module_type != ModuleType.CURATED:
            raise
        addresses = cfg.contract_addresses
    cfg = replace(cfg, contract_addresses=addresses)
    set_config(cfg)
    module_adapter = build_module_adapter_from_config(cfg, w3, SharedChainConnection(w3))
    return EventReplayHarness(persistent_w3, w3, backfill_w3, module_adapter)


async def build_module_supervisor(ws_url: str, http_url: str) -> "ModuleSupervisorHarness":
    from sentinel.config import get_config, set_config
    from sentinel.app.module_adapter import build_module_adapter_from_config
    from sentinel.chain import SharedChainConnection

    persistent_w3 = AsyncWeb3(WebSocketProvider(ws_url, max_connection_retries=-1))
    backfill_w3 = AsyncWeb3(AsyncHTTPProvider(http_url))
    w3 = AsyncWeb3(WebSocketProvider(ws_url, max_connection_retries=-1))
    cfg = get_config()
    addresses = await discover_contract_addresses_from_url(http_url, cfg.contract_addresses.module)
    cfg = replace(cfg, contract_addresses=addresses)
    set_config(cfg)
    chain = SharedChainConnection(w3)
    module_adapter = build_module_adapter_from_config(cfg, w3, chain)
    return ModuleSupervisorHarness(persistent_w3, w3, backfill_w3, cfg, chain, module_adapter)


class EventReplayHarness:
    """Minimal replay helper using the production module runtime wiring."""

    def __init__(
        self,
        persistent_w3: AsyncWeb3,
        w3: AsyncWeb3,
        backfill_w3: AsyncWeb3,
        module_adapter,
    ) -> None:
        self._storage = _InMemoryProcessingStateProvider()
        self._notification_sink = _RecordingNotificationSink()
        self.runtime = build_module_runtime(
            persistent_w3,
            health=HealthState(),
            module_adapter=module_adapter,
            storage=self._storage,
            notification_sink=self._notification_sink,
            backfill_w3=backfill_w3,
        )
        self.raw_subscription = self.runtime.raw_subscription
        self._notification_sink.event_messages = self.runtime.event_messages
        self._backfill_w3 = backfill_w3
        self.processed_events = self._notification_sink.processed_events

    async def replay_blocks(self, start_block: int, end_block: int | None = None):
        await self.raw_subscription.replay_blocks(start_block, end_block=end_block)

    async def subscribe(self) -> None:
        await self.raw_subscription.subscribe()

    async def wait_until_subscribed(self, *, timeout: float = 10.0) -> None:
        await self.raw_subscription.wait_until_subscribed(timeout=timeout)

    async def stop(self) -> None:
        await self.raw_subscription.stop()

    async def disconnect(self) -> None:
        event_messages_w3 = getattr(getattr(self.runtime.event_messages, "chain", None), "w3", None)
        providers = [
            self.raw_subscription._w3.provider,
            self._backfill_w3.provider,
            event_messages_w3.provider if event_messages_w3 is not None else None,
        ]
        for provider in providers:
            if provider is None or not hasattr(provider, "disconnect"):
                continue
            with suppress(Exception):
                await provider.disconnect()


class _InMemoryProcessingStateProvider:
    def __init__(self) -> None:
        self._bot_data: dict = {}

    @property
    def state(self) -> BotStorage:
        return BotStorage(self._bot_data)


class _RecordingNotificationSink:
    def __init__(self) -> None:
        self.event_messages = None
        self.event_messages_provider = None
        self.processed_events = []

    async def emit(self, notification: EventNotification) -> None:
        event_messages = (
            self.event_messages_provider()
            if self.event_messages_provider is not None
            else self.event_messages
        )
        if event_messages is None:
            raise RuntimeError("Recording notification sink is not bound")
        source_txs = list(dict.fromkeys(event.tx for event in notification.source_events))
        stable_txs = {
            tx: HexBytes("0xdeadbeef" if index == 0 else f"0x{index:08x}")
            for index, tx in enumerate(source_txs)
        }
        for event in notification.source_events:
            event.tx = stable_txs[event.tx]
        plan = await event_messages.get_notification_plan(notification)
        for event in notification.source_events:
            self.processed_events.append((event, plan))


class ModuleSupervisorHarness:
    """Integration helper using the production module supervisor lifecycle."""

    def __init__(
        self,
        persistent_w3: AsyncWeb3,
        w3: AsyncWeb3,
        backfill_w3: AsyncWeb3,
        cfg,
        chain,
        module_adapter,
    ) -> None:
        self._storage = _InMemoryProcessingStateProvider()
        self._notification_sink = _RecordingNotificationSink()
        self._backfill_w3 = backfill_w3
        self._chain = chain
        self.supervisor = ModuleRuntimeSupervisor(
            lambda: persistent_w3,
            config=cfg,
            chain=chain,
            health=HealthState(),
            module_adapter=module_adapter,
            storage=self._storage,
            notification_sink=self._notification_sink,
            backfill_w3=backfill_w3,
        )
        self._notification_sink.event_messages_provider = lambda: self.supervisor.event_messages
        self.processed_events = self._notification_sink.processed_events

    async def subscribe(self) -> None:
        await self.supervisor.subscribe()

    async def wait_until_subscribed(self, *, timeout: float = 10.0) -> None:
        await self.supervisor.wait_until_subscribed(timeout=timeout)

    async def stop(self) -> None:
        self.supervisor.request_shutdown()
        await self.supervisor.raw_subscription.stop()

    async def disconnect(self) -> None:
        event_messages_w3 = getattr(
            getattr(self.supervisor.event_messages, "chain", None), "w3", None
        )
        providers = [
            self.supervisor.raw_subscription._w3.provider,
            self._backfill_w3.provider,
            self._chain.w3.provider,
            event_messages_w3.provider if event_messages_w3 is not None else None,
        ]
        for provider in providers:
            if provider is None or not hasattr(provider, "disconnect"):
                continue
            with suppress(Exception):
                await provider.disconnect()


def _build_web3(provider_url: str) -> AsyncWeb3:
    if provider_url.startswith("ws://") or provider_url.startswith("wss://"):
        provider = WebSocketProvider(provider_url, max_connection_retries=-1)
    else:
        provider = AsyncHTTPProvider(provider_url)
    return AsyncWeb3(provider)


async def discover_contract_addresses_from_url(provider_url: str, module_address: str):
    from sentinel.app.contracts import discover_contract_addresses

    w3 = _build_web3(provider_url)
    try:
        return await discover_contract_addresses(w3, module_address)
    finally:
        with suppress(Exception):
            await w3.provider.disconnect()


async def replay_transaction_on_anvil(
    *,
    fork_provider_url: str,
    anvil_http_url: str,
    tx_hash: str,
    timeout: float = 120.0,
) -> TxReceipt:
    """Rebroadcast the desired historical transaction on the configured local fork."""

    target_hash = HexBytes(tx_hash)

    fork_w3 = _build_web3(fork_provider_url)
    try:
        provider = fork_w3.provider
        if isinstance(provider, WebSocketProvider) and not await provider.is_connected():
            await provider.connect()
        try:
            tx = await fork_w3.eth.get_transaction(target_hash)
        except TransactionNotFound as exc:
            import pytest

            pytest.skip(f"Transaction not found in fork provider: {tx_hash}")
            raise exc
    finally:
        provider = fork_w3.provider
        if hasattr(provider, "disconnect"):
            with suppress(Exception):
                await provider.disconnect()

    local_w3 = AsyncWeb3(AsyncHTTPProvider(anvil_http_url))

    from_address = tx["from"]
    await local_w3.provider.make_request(RPCEndpoint("anvil_impersonateAccount"), [from_address])
    try:
        params = _build_replay_tx_params(tx)
        submitted_tx_hash = await local_w3.eth.send_transaction(params)
        receipt = await local_w3.eth.wait_for_transaction_receipt(
            submitted_tx_hash, timeout=timeout
        )
    finally:
        with suppress(Exception):
            await local_w3.provider.make_request(
                RPCEndpoint("anvil_stopImpersonatingAccount"), [from_address]
            )
        with suppress(Exception):
            await local_w3.provider.disconnect()

    return receipt


async def mine_anvil_blocks(anvil_http_url: str, count: int) -> None:
    if count <= 0:
        return

    w3 = AsyncWeb3(AsyncHTTPProvider(anvil_http_url))
    try:
        await w3.provider.make_request(RPCEndpoint("anvil_mine"), [hex(count)])
    finally:
        with suppress(Exception):
            await w3.provider.disconnect()


def _build_replay_tx_params(tx) -> TxParams[str, Any]:
    params: TxParams[str, Any] = {
        "from": tx["from"],
        "to": tx["to"],
        "value": tx["value"],
        "data": tx["input"],
        "gas": tx["gas"],
        "nonce": tx["nonce"],
    }

    chain_id = tx.get("chainId")
    if chain_id is not None:
        params["chainId"] = chain_id

    tx_type = tx.get("type")
    if tx_type is not None:
        params["type"] = hex(tx_type) if isinstance(tx_type, int) else tx_type

    max_fee = tx.get("maxFeePerGas")
    if max_fee is not None:
        params["maxFeePerGas"] = max_fee

    max_priority_fee = tx.get("maxPriorityFeePerGas")
    if max_priority_fee is not None:
        params["maxPriorityFeePerGas"] = max_priority_fee

    gas_price = tx.get("gasPrice")
    if tx_type in (None, 0, "0x0", 1, "0x1") and gas_price is not None:
        params["gasPrice"] = gas_price

    access_list = tx.get("accessList")
    if access_list:
        params["accessList"] = access_list

    return params
