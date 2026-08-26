import asyncio
from datetime import UTC, time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

from hexbytes import HexBytes
import pytest

from sentinel.app.health import HealthState
from sentinel.app.contracts import CommunityContractAddresses
from sentinel.app.telegram_adapters import (
    TelegramNotificationHandler,
    TelegramNotificationSink,
)
from sentinel.chain import SharedChainConnection
from sentinel.config import Config, clear_config, set_config
from sentinel.module_types import ModuleType
from sentinel.app.storage import BotStorage
from sentinel.app.storage_migrations import migrate_legacy_storage
from sentinel.models import Block, Event, EventHandler, EventNotification
from sentinel.modules.base import EventSource
from sentinel.notifications import NotificationPlan
from sentinel.rpc_provider import (
    RpcEndpointsUnavailable,
    RpcFailureKind,
    RpcFailureSummary,
    RpcSubscriptionReconnectRequired,
)
from sentinel.modules.aggregation import (
    AggregationGroup,
    AggregationGroups,
    AggregationKey,
    AggregationWindow,
    DistributionReportAggregator,
    GlobalEventAggregator,
    NodeOperatorEventAggregator,
    OperatorGroupChangeAggregator,
)
from sentinel.services.aggregation import AggregationCoordinator
from sentinel.services.digest import Digest, DigestGroups, build_digests
from sentinel.services.subscription import (
    ModuleRuntime,
    ModuleRuntimeSupervisor,
)
from sentinel.rpc import Subscription

DEPOSIT_EVENT = "DepositedSigningKeysCountChanged"
TOTAL_SIGNING_KEYS_COUNT_CHANGED = "TotalSigningKeysCountChanged"


class _FakeEth:
    def contract(self, **kwargs):
        return SimpleNamespace(**kwargs)


class _FakeW3:
    eth = _FakeEth()


class _FakeProvider:
    is_connected = AsyncMock(return_value=True)
    connect = AsyncMock()


class _FakeRawW3:
    provider = _FakeProvider()
    eth = _FakeEth()


class _FakeModuleAdapter:
    def event_sources(self):
        return ()

    def notifiable_events(self):
        return set()

    def side_effect_events(self):
        return set()

    def topic_abis(self):
        return ()


class _FakeEventMessages:
    def __init__(self):
        self.cfg = None
        self.module_adapter = None
        self.event_handlers = {}


class _FakeEventSideEffects:
    def __init__(self):
        self.module_adapter = None
        self.process_event = AsyncMock()


def _make_config() -> Config:
    return Config(
        filestorage_path=".storage",
        token="token",
        web3_socket_providers=("wss://example.invalid",),
        healthcheck_host="0.0.0.0",
        healthcheck_port=8080,
        contract_addresses=CommunityContractAddresses(
            module="0x0000000000000000000000000000000000000001",
            accounting="0x0000000000000000000000000000000000000002",
            parameters_registry="0x0000000000000000000000000000000000000003",
            vebo="0x0000000000000000000000000000000000000004",
            fee_distributor="0x0000000000000000000000000000000000000005",
            exit_penalties="0x0000000000000000000000000000000000000006",
            lido_locator="0x0000000000000000000000000000000000000007",
            staking_router="0x0000000000000000000000000000000000000008",
            staking_module_id=1,
            module_type=ModuleType.COMMUNITY,
        ),
        etherscan_url="https://etherscan.io",
        beaconchain_url="https://beaconcha.in",
        module_ui_url="https://csm.lido.fi",
        block_batch_size=10_000,
        process_blocks_requests_per_second=None,
        block_from=None,
        admin_ids=set(),
        deposit_digest_time=time(9, 0, tzinfo=UTC),
    )


def _make_event(block: int) -> Event:
    return Event(
        event="TestEvent",
        args={"nodeOperatorId": 1},
        block=block,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )


def _make_signing_keys_event(
    *,
    event_name: str = "TotalSigningKeysCountChanged",
    node_operator_id: int = 1,
    count: int,
    block: int = 123,
    tx: str = "0xdeadbeef",
    log_index: int,
) -> Event:
    count_arg = (
        {"totalKeysCount": count}
        if event_name == "TotalSigningKeysCountChanged"
        else {"depositedKeysCount": count}
    )
    return Event(
        event=event_name,
        args={"nodeOperatorId": node_operator_id} | count_arg,
        block=block,
        tx=HexBytes(tx),
        address="0x0000000000000000000000000000000000000000",
        log_index=log_index,
        transaction_index=0,
    )


def _make_operator_group_event(
    event_name: str,
    *,
    group_id: int = 7,
    block: int = 123,
    log_index: int,
    group_info: dict | None = None,
) -> Event:
    args = {"groupId": group_id}
    if group_info is not None:
        args["groupInfo"] = group_info
    return Event(
        event=event_name,
        args=args,
        block=block,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=log_index,
        transaction_index=0,
    )


def _make_distribution_event(
    event_name: str,
    *,
    block: int = 123,
    transaction_index: int = 0,
    log_index: int,
    distributed: int = 1,
) -> Event:
    args = {"shares": distributed} if event_name == "ModuleFeeDistributed" else {"logCid": "cid123"}
    return Event(
        event=event_name,
        args=args,
        block=block,
        tx=HexBytes(f"0x{transaction_index + 1:064x}"),
        address="0x0000000000000000000000000000000000000005",
        log_index=log_index,
        transaction_index=transaction_index,
    )


def _make_context(block: int) -> SimpleNamespace:
    bot_storage = BotStorage({"block": block, "user_ids": set(), "no_ids_to_chats": {}})
    return SimpleNamespace(bot_storage=bot_storage, bot=AsyncMock())


def _make_notification_handler(event_messages_return=None) -> TelegramNotificationHandler:
    event_messages = SimpleNamespace(
        get_notification_plan=AsyncMock(return_value=event_messages_return),
    )
    return TelegramNotificationHandler(
        SimpleNamespace(),
        lambda: event_messages,
    )


def _make_raw_subscription() -> Subscription:
    set_config(_make_config())
    return Subscription(
        _FakeRawW3(),
        health=HealthState(),
        module_adapter=_FakeModuleAdapter(),
    )


class _FakeSubscriptionStorage:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data

    @property
    def state(self) -> BotStorage:
        return BotStorage(self.bot_data)

    def __call__(self) -> BotStorage:
        return self.state


async def _wait_for(predicate, *, timeout: float = 1.0, interval: float = 0.01) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("Timed out waiting for condition")
        await asyncio.sleep(interval)


class _FakeNotificationSink:
    def __init__(self) -> None:
        self.emit = AsyncMock()


def _make_event_messages(
    aggregation_group: AggregationGroup | None = None,
    *,
    event_names: frozenset[str] = frozenset({TOTAL_SIGNING_KEYS_COUNT_CHANGED}),
) -> SimpleNamespace:
    return SimpleNamespace(
        event_handlers={
            event_name: SimpleNamespace(
                event=event_name,
                aggregation_group=aggregation_group,
            )
            for event_name in event_names
        }
    )


def _make_processing_harness(
    *,
    aggregation_group: AggregationGroup | None = None,
    event_names: frozenset[str] = frozenset({TOTAL_SIGNING_KEYS_COUNT_CHANGED}),
    bot_data: dict | None = None,
) -> SimpleNamespace:
    storage = _FakeSubscriptionStorage(bot_data if bot_data is not None else {})
    sink = _FakeNotificationSink()
    aggregators = ()
    if aggregation_group is not None:
        aggregators = (
            NodeOperatorEventAggregator(
                group=aggregation_group,
                event_names=event_names,
            ),
        )
    aggregation = AggregationCoordinator(
        storage=storage,
        emit_notification=sink.emit,
        aggregators=aggregators,
    )
    side_effects = _FakeEventSideEffects()
    digest = Digest(
        DigestGroups.DEPOSITED_SIGNING_KEYS,
        DEPOSIT_EVENT,
        lambda: storage.state.digests,
        sink.emit,
    )
    runtime = ModuleRuntime(
        module_adapter=cast(
            object,
            SimpleNamespace(refresh_staking_module_id=AsyncMock()),
        ),
        raw_subscription=cast(Subscription, SimpleNamespace()),
        storage=storage,
        event_messages=_make_event_messages(aggregation_group, event_names=event_names),
        event_side_effects=side_effects,
        aggregation=aggregation,
        digests={digest.name: digest},
    )
    return SimpleNamespace(
        storage=storage,
        sink=sink,
        side_effects=side_effects,
        aggregation=aggregation,
        digest=digest,
        runtime=runtime,
    )


@pytest.mark.asyncio
async def test_rpc_disconnect_rebuilds_runtime_and_replays_after_persisted_block(monkeypatch):
    from sentinel.app.module_adapter import build_module_adapter_from_config

    cfg = _make_config()
    w3 = _FakeW3()
    replacement_w3 = _FakeW3()
    subscription_w3_factory = Mock(side_effect=[w3, replacement_w3])
    set_config(cfg)
    try:
        chain = SharedChainConnection(w3)
        module_adapter = build_module_adapter_from_config(cfg, w3, chain)
        application = SimpleNamespace(
            bot_data={"block": 123},
            update_queue=SimpleNamespace(put=AsyncMock()),
        )
        supervisor = ModuleRuntimeSupervisor(
            subscription_w3_factory,
            config=cfg,
            chain=chain,
            health=HealthState(),
            module_adapter=module_adapter,
            storage=lambda: BotStorage(application.bot_data),
            emit_notification=TelegramNotificationSink(application).emit,
        )
        original_runtime = supervisor.module_runtime
        original_runtime.raw_subscription.subscribe = AsyncMock(
            side_effect=RpcSubscriptionReconnectRequired.listener_stopped()
        )
        original_runtime.raw_subscription.abort = AsyncMock()

        restarted = await supervisor._subscribe_until_restarted_or_stopped()

        assert restarted is True
        assert supervisor.module_runtime is not original_runtime
        assert supervisor._pending_replay_start_block == 124
        assert supervisor._health.snapshot().catchup_active is True
        original_runtime.raw_subscription.abort.assert_awaited_once()
        assert supervisor.raw_subscription._w3 is replacement_w3  # noqa: SLF001
        assert subscription_w3_factory.call_count == 2

        supervisor.raw_subscription.get_block_number = AsyncMock(return_value=130)
        supervisor.raw_subscription.replay_blocks = AsyncMock()
        await supervisor._replay_pending_blocks_after_restart()

        supervisor.raw_subscription.replay_blocks.assert_awaited_once_with(
            124,
            end_block=130,
            suppress_live_events_until=130,
        )
        assert supervisor._pending_replay_start_block is None
        assert supervisor._health.snapshot().catchup_active is False

        supervisor.raw_subscription.wait_until_subscribed = AsyncMock()
        await supervisor.wait_until_subscribed()
        supervisor.raw_subscription.wait_until_subscribed.assert_awaited_once_with(timeout=None)

        supervisor._pending_replay_start_block = 131
        supervisor._health.mark_catchup_started()
        supervisor.raw_subscription.get_block_number = AsyncMock(return_value=140)
        supervisor.raw_subscription.replay_blocks = AsyncMock(
            side_effect=RpcEndpointsUnavailable("backfill connection lost")
        )

        with pytest.raises(RpcEndpointsUnavailable):
            await supervisor._replay_pending_blocks_after_restart()

        assert supervisor._pending_replay_start_block == 131
        assert supervisor._health.snapshot().catchup_active is True

        monkeypatch.setattr("sentinel.services.subscription.RPC_RECOVERY_RETRY_SECONDS", 0)
        head_error = RpcSubscriptionReconnectRequired(
            "eth_blockNumber",
            RpcFailureSummary(
                endpoint_index=0,
                endpoint_label="rpc-1 (primary.invalid)",
                kind=RpcFailureKind.RPC_REJECTED,
                rpc_code=-32000,
            ),
        )
        supervisor.raw_subscription.get_block_number = AsyncMock(side_effect=[head_error, 130])
        supervisor.raw_subscription.replay_blocks = AsyncMock()

        await supervisor.catch_up_from(124)

        assert supervisor.raw_subscription.get_block_number.await_count == 2
        supervisor.raw_subscription.replay_blocks.assert_awaited_once_with(
            124,
            end_block=130,
            suppress_live_events_until=130,
        )

        replay_attempt = 0

        async def replay_with_transient_rpc_failure(*args, **kwargs):
            nonlocal replay_attempt
            replay_attempt += 1
            if replay_attempt == 1:
                application.bot_data["block"] = 125
                raise RpcEndpointsUnavailable("provider-specific error")

        supervisor.raw_subscription.get_block_number = AsyncMock(return_value=130)
        supervisor.raw_subscription.replay_blocks = AsyncMock(
            side_effect=replay_with_transient_rpc_failure
        )

        await supervisor.catch_up_from(124)

        replay_calls = supervisor.raw_subscription.replay_blocks.await_args_list
        assert replay_calls[0].args == (124,)
        assert replay_calls[1].args == (126,)
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_module_runtime_dispatches_side_effects_before_notification_sink():
    harness = _make_processing_harness(aggregation_group=None, event_names=frozenset())
    calls = []
    harness.side_effects.process_event.side_effect = lambda event: calls.append("side_effects")
    harness.sink.emit.side_effect = lambda event: calls.append("notification")

    await harness.runtime.handle_event(_make_event(block=1))

    assert calls == ["side_effects", "notification"]


@pytest.mark.asyncio
async def test_module_runtime_queues_non_aggregated_event_notification():
    harness = _make_processing_harness(aggregation_group=None, event_names=frozenset())
    event = _make_event(block=1)

    await harness.runtime.handle_event(event)

    harness.sink.emit.assert_awaited_once()
    emitted = harness.sink.emit.await_args.args[0]
    assert isinstance(emitted, EventNotification)
    assert emitted.source_events == (event,)


@pytest.mark.asyncio
async def test_module_runtime_buffers_deposit_until_digest_flush(caplog):
    event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        count=1,
        block=123,
        log_index=1,
    )
    harness = _make_processing_harness(aggregation_group=None)
    with caplog.at_level("INFO"):
        await harness.runtime.handle_event(event)
        notification_count = await harness.digest.flush_through(event.block)

    processed = next(
        record for record in caplog.records if record.getMessage() == "Event processed"
    )
    assert processed.event_name == DEPOSIT_EVENT
    assert processed.block == event.block
    assert processed.log_index == event.log_index
    assert notification_count == 1
    harness.sink.emit.assert_awaited_once()


@pytest.mark.asyncio
async def test_subscription_catchup_suppresses_duplicate_live_events_only():
    try:
        subscription = _make_raw_subscription()
        consumer = SimpleNamespace(handle_event=AsyncMock())
        subscription.add_event_consumer(consumer)

        subscription._ignore_subscription_events_until_block = 100

        await subscription._emit_subscription_event(_make_event(block=99))
        consumer.handle_event.assert_not_awaited()

        await subscription._emit_event(_make_event(block=99))
        consumer.handle_event.assert_awaited_once()

        subscription._ignore_subscription_events_until_block = 98
        await subscription._emit_subscription_event(_make_event(block=100))

        assert consumer.handle_event.await_count == 2
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_replay_blocks_flushes_buffered_live_events_after_replayed_events():
    try:
        subscription = _make_raw_subscription()
        calls: list[tuple[str, int]] = []
        event_consumer = SimpleNamespace(
            handle_event=AsyncMock(side_effect=lambda event: calls.append(("event", event.block)))
        )
        block_consumer = SimpleNamespace(
            handle_block=AsyncMock(side_effect=lambda block: calls.append(("block", block.number)))
        )
        subscription.add_event_consumer(event_consumer)
        subscription.add_block_consumer(block_consumer)

        replayed_event = _make_event(block=100)
        live_event = _make_event(block=101)

        class FakeEventLogReader:
            async def connected_w3(self):
                return SimpleNamespace(
                    eth=SimpleNamespace(get_block_number=AsyncMock(return_value=100))
                )

            async def fetch_events(self, *, start_block: int, end_block: int):
                assert (start_block, end_block) == (100, 100)
                await subscription._emit_subscription_event(live_event)
                return [replayed_event]

        subscription._event_log_reader = FakeEventLogReader()

        await subscription.replay_blocks(100, 100, suppress_live_events_until=100)

        assert calls == [("event", 100), ("block", 100), ("event", 101)]
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_replay_blocks_flushes_live_items_in_block_order():
    try:
        subscription = _make_raw_subscription()
        calls: list[tuple[str, int]] = []
        event_consumer = SimpleNamespace(
            handle_event=AsyncMock(side_effect=lambda event: calls.append(("event", event.block)))
        )
        block_consumer = SimpleNamespace(
            handle_block=AsyncMock(side_effect=lambda block: calls.append(("block", block.number)))
        )
        subscription.add_event_consumer(event_consumer)
        subscription.add_block_consumer(block_consumer)

        class FakeEventLogReader:
            async def connected_w3(self):
                return SimpleNamespace(
                    eth=SimpleNamespace(get_block_number=AsyncMock(return_value=100))
                )

            async def fetch_events(self, *, start_block: int, end_block: int):
                await subscription._emit_subscription_event(_make_event(block=102))
                await subscription._emit_subscription_block(Block(number=102))
                await subscription._emit_subscription_event(_make_event(block=101))
                await subscription._emit_subscription_block(Block(number=101))
                return []

        subscription._event_log_reader = FakeEventLogReader()

        await subscription.replay_blocks(100, 100, suppress_live_events_until=100)

        assert calls == [
            ("block", 100),
            ("event", 101),
            ("block", 101),
            ("event", 102),
            ("block", 102),
        ]
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_replay_blocks_drains_event_received_during_buffer_flush():
    try:
        subscription = _make_raw_subscription()
        calls: list[tuple[str, int]] = []
        late_event = _make_event(block=102)

        async def handle_block(block):
            calls.append(("block", block.number))
            if block.number == 101:
                await subscription._emit_subscription_event(late_event)

        subscription.add_event_consumer(
            SimpleNamespace(
                handle_event=AsyncMock(
                    side_effect=lambda event: calls.append(("event", event.block))
                )
            )
        )
        subscription.add_block_consumer(
            SimpleNamespace(handle_block=AsyncMock(side_effect=handle_block))
        )

        class FakeEventLogReader:
            async def connected_w3(self):
                return SimpleNamespace(
                    eth=SimpleNamespace(get_block_number=AsyncMock(return_value=100))
                )

            async def fetch_events(self, *, start_block: int, end_block: int):
                await subscription._emit_subscription_block(Block(number=101))
                return []

        subscription._event_log_reader = FakeEventLogReader()

        await subscription.replay_blocks(100, 100, suppress_live_events_until=100)

        assert calls == [("block", 100), ("block", 101), ("event", 102)]
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_replay_blocks_keeps_suppressing_delayed_live_duplicates():
    try:
        subscription = _make_raw_subscription()
        consumer = SimpleNamespace(handle_event=AsyncMock())
        subscription.add_event_consumer(consumer)

        class FakeEventLogReader:
            async def connected_w3(self):
                return SimpleNamespace(
                    eth=SimpleNamespace(get_block_number=AsyncMock(return_value=100))
                )

            async def fetch_events(self, *, start_block: int, end_block: int):
                return []

        subscription._event_log_reader = FakeEventLogReader()

        await subscription.replay_blocks(100, 100, suppress_live_events_until=100)
        await subscription._emit_subscription_event(_make_event(block=100))
        await subscription._emit_subscription_event(_make_event(block=101))

        consumer.handle_event.assert_awaited_once()
        assert consumer.handle_event.await_args.args[0].block == 101
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_replay_blocks_discards_buffered_live_events_when_replay_fails():
    try:
        subscription = _make_raw_subscription()
        consumer = SimpleNamespace(handle_event=AsyncMock())
        subscription.add_event_consumer(consumer)
        live_event = _make_event(block=101)

        class FakeEventLogReader:
            async def connected_w3(self):
                return SimpleNamespace(
                    eth=SimpleNamespace(get_block_number=AsyncMock(return_value=100))
                )

            async def fetch_events(self, *, start_block: int, end_block: int):
                assert (start_block, end_block) == (100, 100)
                await subscription._emit_subscription_event(live_event)
                raise RuntimeError("replay interrupted")

        subscription._event_log_reader = FakeEventLogReader()

        with pytest.raises(RuntimeError, match="replay interrupted"):
            await subscription.replay_blocks(100, 100, suppress_live_events_until=100)

        consumer.handle_event.assert_not_awaited()
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_subscription_fans_out_events_and_blocks_to_all_consumers():
    try:
        subscription = _make_raw_subscription()
        event_consumer_1 = SimpleNamespace(handle_event=AsyncMock())
        event_consumer_2 = SimpleNamespace(handle_event=AsyncMock())
        block_consumer_1 = SimpleNamespace(handle_block=AsyncMock())
        block_consumer_2 = SimpleNamespace(handle_block=AsyncMock())
        subscription.add_event_consumer(event_consumer_1)
        subscription.add_event_consumer(event_consumer_2)
        subscription.add_block_consumer(block_consumer_1)
        subscription.add_block_consumer(block_consumer_2)
        event = _make_event(block=123)
        block = Block(number=123)

        await subscription._emit_event(event)
        await subscription._emit_block(block)

        event_consumer_1.handle_event.assert_awaited_once_with(event)
        event_consumer_2.handle_event.assert_awaited_once_with(event)
        block_consumer_1.handle_block.assert_awaited_once_with(block)
        block_consumer_2.handle_block.assert_awaited_once_with(block)
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_new_head_marks_previous_block_as_complete():
    try:
        subscription = _make_raw_subscription()
        block_consumer = SimpleNamespace(handle_block=AsyncMock())
        subscription.add_block_consumer(block_consumer)

        await subscription._handle_new_head_subscription(SimpleNamespace(result={"number": "0x65"}))

        block_consumer.handle_block.assert_awaited_once_with(Block(number=100))
    finally:
        clear_config()


def test_subscription_handlers_are_explicitly_sequential():
    try:
        subscription = _make_raw_subscription()
        subscription._event_sources = (  # noqa: SLF001
            EventSource(
                "module",
                "0x0000000000000000000000000000000000000001",
            ),
        )

        subscriptions = subscription._build_subscriptions()  # noqa: SLF001

        assert len(subscriptions) == 2
        assert all(item.parallelize is False for item in subscriptions)
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_subscription_add_remove_consumers_updates_fanout_registry():
    try:
        subscription = _make_raw_subscription()
        consumer = SimpleNamespace(handle_event=AsyncMock())

        subscription.add_event_consumer(consumer)
        subscription.add_event_consumer(consumer)
        await subscription._emit_event(_make_event(block=1))

        consumer.handle_event.assert_awaited_once()

        subscription.remove_event_consumer(consumer)
        subscription.remove_event_consumer(consumer)
        await subscription._emit_event(_make_event(block=2))

        consumer.handle_event.assert_awaited_once()
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_subscription_shutdown_suppresses_event_and_block_delivery():
    try:
        subscription = _make_raw_subscription()
        event_consumer = SimpleNamespace(handle_event=AsyncMock())
        block_consumer = SimpleNamespace(handle_block=AsyncMock())
        subscription.add_event_consumer(event_consumer)
        subscription.add_block_consumer(block_consumer)

        subscription.request_shutdown()
        await subscription._emit_subscription_event(_make_event(block=1))
        await subscription._emit_event(_make_event(block=2))
        await subscription._emit_block(Block(number=2))

        event_consumer.handle_event.assert_not_awaited()
        block_consumer.handle_block.assert_not_awaited()
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_subscription_stop_is_idempotent_for_concurrent_shutdowns():
    try:
        subscription = _make_raw_subscription()
        unsubscribe_all = AsyncMock()
        subscription._w3.subscription_manager = SimpleNamespace(  # noqa: SLF001
            unsubscribe_all=unsubscribe_all,
        )

        await asyncio.gather(subscription.stop(), subscription.stop())

        unsubscribe_all.assert_awaited_once()
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_subscription_stop_does_not_hide_unexpected_value_error():
    try:
        subscription = _make_raw_subscription()
        unsubscribe_all = AsyncMock(side_effect=ValueError("list.remove(x): x not in list"))
        subscription._w3.subscription_manager = SimpleNamespace(  # noqa: SLF001
            unsubscribe_all=unsubscribe_all,
        )

        with pytest.raises(ValueError, match="list.remove"):
            await subscription.stop()

        unsubscribe_all.assert_awaited_once()
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_subscription_abort_does_not_unsubscribe_stale_ids():
    try:
        subscription = _make_raw_subscription()
        unsubscribe_all = AsyncMock()
        disconnect = AsyncMock()
        subscription._w3.subscription_manager = SimpleNamespace(  # noqa: SLF001
            unsubscribe_all=unsubscribe_all,
        )
        subscription._w3.provider = SimpleNamespace(disconnect=disconnect)  # noqa: SLF001

        await subscription.abort()

        unsubscribe_all.assert_not_awaited()
        disconnect.assert_awaited_once()
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_total_signing_key_count_events_are_aggregated_once_per_block():
    block_events = [
        _make_signing_keys_event(count=1, log_index=1),
        _make_signing_keys_event(count=3, tx="0xfeedbeef", log_index=3),
        _make_signing_keys_event(
            node_operator_id=2,
            count=4,
            log_index=4,
        ),
    ]
    harness = _make_processing_harness(
        aggregation_group=AggregationGroups.TOTAL_SIGNING_KEY_COUNTS,
    )

    for event in block_events:
        await harness.runtime.handle_event(event)
    await harness.runtime.handle_block(Block(number=123))

    prepared = [call.args[0] for call in harness.sink.emit.await_args_list]
    assert len(prepared) == 2
    assert all(isinstance(event, EventNotification) for event in prepared)
    prepared_by_key = {(event.event, event.args["nodeOperatorId"]): event for event in prepared}
    assert prepared_by_key[("TotalSigningKeysCountChanged", 1)].args == {
        "nodeOperatorId": 1,
        "totalKeysCount": 3,
    }
    assert prepared_by_key[("TotalSigningKeysCountChanged", 1)].source_events == (
        block_events[0],
        block_events[1],
    )
    assert prepared_by_key[("TotalSigningKeysCountChanged", 2)].args == {
        "nodeOperatorId": 2,
        "totalKeysCount": 4,
    }
    assert harness.storage.state.block.value == 123


@pytest.mark.asyncio
async def test_distribution_report_events_are_aggregated_in_one_block_window():
    storage = _FakeSubscriptionStorage({})
    sink = _FakeNotificationSink()
    aggregation = AggregationCoordinator(
        storage=storage,
        emit_notification=sink.emit,
        aggregators=(DistributionReportAggregator(),),
    )
    first_report = (
        _make_distribution_event(
            "ModuleFeeDistributed",
            transaction_index=4,
            log_index=10,
            distributed=0,
        ),
        _make_distribution_event(
            "DistributionLogUpdated",
            transaction_index=4,
            log_index=11,
        ),
    )
    for event in reversed(first_report):
        await aggregation.handle_event(event)

    assert aggregation.pending_window_count == 1
    (window,) = storage.state.aggregation_windows.pending()
    assert window.aggregation_key == AggregationKey.global_key()
    sink.emit.assert_not_awaited()

    await aggregation.handle_block(123)

    notification = sink.emit.await_args.args[0]
    assert notification.source_events == first_report
    assert notification.event == "DistributionLogUpdated"
    assert aggregation.pending_window_count == 0


def test_distribution_report_aggregator_warns_about_incomplete_pair(caplog):
    event = _make_distribution_event(
        "ModuleFeeDistributed",
        transaction_index=4,
        log_index=10,
    )

    with caplog.at_level("WARNING"):
        notifications = DistributionReportAggregator().aggregate((event,))

    assert notifications == []
    assert "Incomplete distribution report aggregation" in caplog.text
    assert caplog.records[-1].missing_event_names == ["DistributionLogUpdated"]


def test_distribution_report_aggregator_warns_about_cross_transaction_pair(caplog):
    events = (
        _make_distribution_event(
            "ModuleFeeDistributed",
            transaction_index=4,
            log_index=10,
        ),
        _make_distribution_event(
            "DistributionLogUpdated",
            transaction_index=7,
            log_index=11,
        ),
    )

    with caplog.at_level("WARNING"):
        notifications = DistributionReportAggregator().aggregate(events)

    assert notifications == []
    assert "Distribution report events span multiple transactions" in caplog.text


@pytest.mark.asyncio
async def test_deposit_digest_flushes_completed_events_and_keeps_newer_events():
    first_event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        count=1,
        block=123,
        log_index=1,
    )
    last_ready_event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        count=2,
        block=125,
        tx="0xfeedbeef",
        log_index=2,
    )
    other_operator_event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        node_operator_id=2,
        count=4,
        block=124,
        tx="0xcafebabe",
        log_index=3,
    )
    newer_event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        count=3,
        block=126,
        tx="0xfacefeed",
        log_index=4,
    )
    harness = _make_processing_harness(aggregation_group=None)

    for event in (first_event, other_operator_event, last_ready_event, newer_event):
        await harness.runtime.handle_event(event)
    notification_count = await harness.digest.flush_through(last_ready_event.block)

    assert notification_count == 1
    prepared = [call.args[0] for call in harness.sink.emit.await_args_list]
    assert len(prepared) == 1
    assert prepared[0].event == DEPOSIT_EVENT
    assert prepared[0].args == {"nodeOperatorId": 1, "depositedKeysCount": 2}
    assert prepared[0].source_events == (
        first_event,
        other_operator_event,
        last_ready_event,
    )
    assert harness.storage.state.digests.events(DigestGroups.DEPOSITED_SIGNING_KEYS) == (
        newer_event,
    )


@pytest.mark.asyncio
async def test_deposit_digest_keeps_events_when_emit_fails():
    event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        count=1,
        block=123,
        log_index=1,
    )
    harness = _make_processing_harness(aggregation_group=None)
    await harness.runtime.handle_event(event)
    harness.sink.emit.side_effect = RuntimeError("queue unavailable")

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await harness.digest.flush_through(event.block)

    assert harness.storage.state.digests.events(DigestGroups.DEPOSITED_SIGNING_KEYS) == (event,)


@pytest.mark.asyncio
async def test_deposit_digest_does_not_discard_event_added_during_emit():
    ready_event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        count=1,
        block=123,
        log_index=1,
    )
    newer_event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        count=2,
        block=124,
        tx="0xfeedbeef",
        log_index=2,
    )
    harness = _make_processing_harness(aggregation_group=None)
    replacement_digest = Digest(
        DigestGroups.DEPOSITED_SIGNING_KEYS,
        DEPOSIT_EVENT,
        lambda: harness.storage.state.digests,
        harness.sink.emit,
    )

    async def append_from_replacement_runtime(_notification: EventNotification) -> None:
        await replacement_digest.handle_event(newer_event)

    await harness.digest.handle_event(ready_event)
    harness.sink.emit.side_effect = append_from_replacement_runtime

    await harness.digest.flush_through(ready_event.block)

    assert harness.storage.state.digests.events(DigestGroups.DEPOSITED_SIGNING_KEYS) == (
        newer_event,
    )


@pytest.mark.asyncio
async def test_named_digests_keep_and_flush_events_independently():
    other_digest_name = "operator_updates"
    deposit_event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        count=1,
        block=123,
        log_index=1,
    )
    other_event = _make_signing_keys_event(
        event_name=TOTAL_SIGNING_KEYS_COUNT_CHANGED,
        count=2,
        block=124,
        tx="0xfeedbeef",
        log_index=2,
    )
    harness = _make_processing_harness(aggregation_group=None)
    other_digest = Digest(
        other_digest_name,
        TOTAL_SIGNING_KEYS_COUNT_CHANGED,
        lambda: harness.storage.state.digests,
        harness.sink.emit,
    )
    harness.runtime.digests[other_digest.name] = other_digest

    await harness.runtime.handle_event(deposit_event)
    await harness.runtime.handle_event(other_event)
    await harness.digest.flush_through(other_event.block)

    assert harness.storage.state.digests.events(DigestGroups.DEPOSITED_SIGNING_KEYS) == ()
    assert harness.storage.state.digests.events(other_digest_name) == (other_event,)


def test_build_digests_rejects_multiple_event_types_for_one_digest():
    bot_data: dict = {}
    event_handlers = {
        event_name: SimpleNamespace(
            event=event_name,
            digest_name="shared_digest",
        )
        for event_name in (DEPOSIT_EVENT, TOTAL_SIGNING_KEYS_COUNT_CHANGED)
    }

    with pytest.raises(RuntimeError, match="assigned to multiple events"):
        build_digests(
            event_handlers,
            lambda: BotStorage(bot_data).digests,
            AsyncMock(),
        )


def test_event_handler_rejects_aggregation_and_digest_buffering_together():
    with pytest.raises(ValueError, match="cannot use both aggregation and digest buffering"):
        EventHandler(
            event=DEPOSIT_EVENT,
            handler=AsyncMock(),
            aggregation_group=AggregationGroups.TOTAL_SIGNING_KEY_COUNTS,
            digest_name=DigestGroups.DEPOSITED_SIGNING_KEYS,
        )


def test_global_event_aggregator_batches_all_operators_in_one_block():
    aggregator = GlobalEventAggregator(
        group=AggregationGroups.NODE_OPERATOR_EFFECTIVE_WEIGHT_CHANGES,
        event_names=frozenset({"NodeOperatorEffectiveWeightChanged"}),
    )
    events = [
        Event(
            event="NodeOperatorEffectiveWeightChanged",
            args={"nodeOperatorId": node_operator_id, "oldWeight": 1, "newWeight": 2},
            block=123,
            tx=HexBytes(node_operator_id),
            address="0x0000000000000000000000000000000000000000",
            log_index=node_operator_id,
            transaction_index=0,
        )
        for node_operator_id in (10, 11)
    ]

    notifications = aggregator.aggregate(reversed(events))

    assert len(notifications) == 1
    assert notifications[0].source_events == tuple(events)


def test_operator_group_aggregator_only_accepts_group_state_events():
    aggregator = OperatorGroupChangeAggregator()
    events = [
        _make_operator_group_event(
            "NodeOperatorEffectiveWeightChanged",
            log_index=1,
            group_info=None,
        ),
        _make_operator_group_event(
            "BondCurveWeightSet",
            log_index=2,
            group_info=None,
        ),
    ]

    assert aggregator.event_names == {
        "OperatorGroupCreated",
        "OperatorGroupUpdated",
        "OperatorGroupCleared",
    }
    assert aggregator.aggregate(events) == []


def test_operator_group_aggregator_collapses_clear_and_create_into_update_diff():
    recreated_group = {
        "name": "New Group",
        "subNodeOperators": [
            {"nodeOperatorId": 10, "share": 10_000},
        ],
    }
    events = [
        _make_operator_group_event("OperatorGroupCleared", group_id=7, log_index=1),
        _make_operator_group_event(
            "OperatorGroupCreated",
            group_id=7,
            group_info=recreated_group,
            log_index=3,
        ),
    ]

    notifications = OperatorGroupChangeAggregator().aggregate(events)

    assert len(notifications) == 1
    assert notifications[0].event == "OperatorGroupUpdated"
    assert notifications[0].args == {
        "groupId": 7,
        "groupInfo": recreated_group,
    }


def test_operator_group_aggregator_collapses_intermediate_and_final_updates():
    intermediate_event = _make_operator_group_event(
        "OperatorGroupUpdated",
        group_id=7,
        group_info={"name": "Group", "subNodeOperators": []},
        log_index=1,
    )
    final_group = {
        "name": "Group",
        "subNodeOperators": [{"nodeOperatorId": 10, "share": 10_000}],
    }
    final_event = _make_operator_group_event(
        "OperatorGroupUpdated",
        group_id=7,
        group_info=final_group,
        log_index=2,
    )

    notifications = OperatorGroupChangeAggregator().aggregate([intermediate_event, final_event])

    assert len(notifications) == 1
    assert notifications[0].source_events == (intermediate_event, final_event)
    assert notifications[0].args == {"groupId": 7, "groupInfo": final_group}


@pytest.mark.asyncio
async def test_aggregation_window_remains_pending_when_emit_fails():
    block_events = [
        _make_signing_keys_event(count=1, log_index=1),
        _make_signing_keys_event(count=2, log_index=2),
    ]
    bot_data = {}
    harness = _make_processing_harness(
        aggregation_group=AggregationGroups.TOTAL_SIGNING_KEY_COUNTS,
        bot_data=bot_data,
    )
    harness.sink.emit.side_effect = RuntimeError("queue unavailable")

    for event in block_events:
        await harness.runtime.handle_event(event)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await harness.runtime.handle_block(Block(number=123))

    event_names = frozenset({TOTAL_SIGNING_KEYS_COUNT_CHANGED})
    window = NodeOperatorEventAggregator(
        group=AggregationGroups.TOTAL_SIGNING_KEY_COUNTS,
        event_names=event_names,
    ).window_for(block_events[0])
    window = window.with_event(block_events[0]).with_event(block_events[1])
    store = BotStorage(bot_data).aggregation_windows
    assert store.pending() == [window]


@pytest.mark.asyncio
async def test_explicit_replay_emits_completed_aggregation_again():
    event = _make_signing_keys_event(count=1, log_index=1)
    harness = _make_processing_harness(
        aggregation_group=AggregationGroups.TOTAL_SIGNING_KEY_COUNTS,
    )

    await harness.aggregation.handle_event(event)
    await harness.aggregation.handle_block(event.block)
    await harness.aggregation.handle_event(event)
    await harness.aggregation.handle_block(event.block)

    assert harness.sink.emit.await_count == 2


@pytest.mark.asyncio
async def test_explicit_replay_closes_multi_block_window_on_replayed_block():
    aggregation_group = AggregationGroup(name="replayed_group", window_blocks=3)
    first_event = _make_signing_keys_event(count=1, block=100, log_index=1)
    last_event = _make_signing_keys_event(count=2, block=102, log_index=2)
    harness = _make_processing_harness(
        aggregation_group=aggregation_group,
        bot_data={"block": 500},
    )

    await harness.aggregation.handle_event(first_event)
    await harness.aggregation.handle_event(last_event)

    harness.sink.emit.assert_not_awaited()
    await harness.aggregation.handle_block(102)

    harness.sink.emit.assert_awaited_once()
    emitted = harness.sink.emit.await_args.args[0]
    assert emitted.source_events == (first_event, last_event)


@pytest.mark.asyncio
async def test_multi_block_aggregation_window_schedules_lazy_flush():
    aggregation_group = AggregationGroup(
        name="total_signing_key_counts",
        window_blocks=3,
    )
    aggregator = NodeOperatorEventAggregator(
        group=aggregation_group,
        event_names=frozenset({TOTAL_SIGNING_KEYS_COUNT_CHANGED}),
    )
    event = _make_signing_keys_event(count=1, block=100, log_index=1)
    later_event = _make_signing_keys_event(count=2, block=101, log_index=2)
    harness = _make_processing_harness(
        aggregation_group=aggregation_group,
        event_names=frozenset({TOTAL_SIGNING_KEYS_COUNT_CHANGED}),
    )

    await harness.aggregation.handle_event(event)
    await harness.aggregation.handle_event(later_event)

    harness.sink.emit.assert_not_awaited()
    assert harness.storage.state.aggregation_windows.pending() == [
        aggregator.window_for(event).with_event(event).with_event(later_event)
    ]
    await harness.aggregation.handle_block(102)
    harness.sink.emit.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggregation_window_deduplicates_replayed_event():
    event = _make_signing_keys_event(count=1, block=100, log_index=1)
    harness = _make_processing_harness(
        aggregation_group=AggregationGroups.TOTAL_SIGNING_KEY_COUNTS,
    )

    await harness.aggregation.handle_event(event)
    await harness.aggregation.handle_event(event)
    await harness.aggregation.handle_block(100)

    emitted = harness.sink.emit.await_args.args[0]
    assert emitted.source_events == (event,)


@pytest.mark.asyncio
async def test_pending_aggregation_window_resumes_from_persisted_state():
    aggregation_group = AggregationGroup(
        name="total_signing_key_counts",
        window_blocks=3,
    )
    aggregator = NodeOperatorEventAggregator(
        group=aggregation_group,
        event_names=frozenset({TOTAL_SIGNING_KEYS_COUNT_CHANGED}),
    )
    first_event = _make_signing_keys_event(count=1, block=100, log_index=1)
    second_event = _make_signing_keys_event(count=2, block=102, log_index=2)
    window = aggregator.window_for(first_event).with_event(first_event).with_event(second_event)
    bot_data = {}
    store = BotStorage(bot_data).aggregation_windows
    store.upsert_pending(window)
    bot_data["block"] = 102
    harness = _make_processing_harness(
        aggregation_group=aggregation_group,
        event_names=aggregator.event_names,
        bot_data=bot_data,
    )

    await harness.aggregation.resume_pending()

    assert store.pending() == []
    harness.sink.emit.assert_awaited_once()
    emitted = harness.sink.emit.await_args.args[0]
    assert isinstance(emitted, EventNotification)
    assert emitted.source_events == (first_event, second_event)


def test_legacy_deposit_windows_are_migrated_to_digest_store():
    first_event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        count=1,
        block=100,
        log_index=1,
    )
    second_event = _make_signing_keys_event(
        event_name=DEPOSIT_EVENT,
        node_operator_id=2,
        count=4,
        block=101,
        tx="0xfeedbeef",
        log_index=2,
    )
    bot_data = {
        "aggregation_windows": {
            "first": {
                "group": "deposited_signing_key_counts",
                "events": [first_event.to_dict()],
            },
            "second": {
                "group": "deposited_signing_key_counts",
                "events": [second_event.to_dict()],
            },
        }
    }

    migrate_legacy_storage(bot_data)
    storage = BotStorage(bot_data)

    assert storage.digests.events(DigestGroups.DEPOSITED_SIGNING_KEYS) == (
        first_event,
        second_event,
    )
    assert storage.aggregation_windows.pending() == []


@pytest.mark.asyncio
async def test_unknown_persisted_aggregation_window_is_discarded(caplog):
    window = AggregationWindow(
        group="removed_group",
        aggregation_key=AggregationKey.global_key(),
        start_block=100,
        end_block=100,
        event_names=frozenset({"RemovedEvent"}),
    )
    harness = _make_processing_harness(
        aggregation_group=None,
        event_names=frozenset(),
    )
    store = harness.storage.state.aggregation_windows
    store.upsert_pending(window)

    with caplog.at_level("WARNING"):
        await harness.aggregation.handle_block(100)

    assert store.pending() == []
    assert "Discarding aggregation window without registered aggregator" in caplog.text
    harness.sink.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_aggregation_uses_replaced_application_bot_data():
    from sentinel.app.module_adapter import build_module_adapter_from_config

    cfg = _make_config()
    w3 = _FakeW3()
    set_config(cfg)
    try:
        chain = SharedChainConnection(w3)
        module_adapter = build_module_adapter_from_config(cfg, w3, chain)
        aggregation_group = AggregationGroup(
            name="total_signing_key_counts",
            window_blocks=3,
        )
        module_adapter.event_aggregators = lambda: (
            NodeOperatorEventAggregator(
                group=aggregation_group,
                event_names=frozenset({TOTAL_SIGNING_KEYS_COUNT_CHANGED}),
            ),
        )
        initial_bot_data = {}
        application = SimpleNamespace(
            bot_data=initial_bot_data,
            update_queue=SimpleNamespace(put=AsyncMock()),
        )
        subscription = ModuleRuntimeSupervisor(
            lambda: w3,
            config=cfg,
            chain=chain,
            health=HealthState(),
            module_adapter=module_adapter,
            storage=lambda: BotStorage(application.bot_data),
            emit_notification=TelegramNotificationSink(application).emit,
        )

        persisted_bot_data = {"block": 102}
        application.bot_data = persisted_bot_data
        aggregator = NodeOperatorEventAggregator(
            group=aggregation_group,
            event_names=frozenset({TOTAL_SIGNING_KEYS_COUNT_CHANGED}),
        )
        block_events = [
            _make_signing_keys_event(count=1, block=100, log_index=1),
            _make_signing_keys_event(count=2, block=102, log_index=2),
        ]
        window = aggregator.window_for(block_events[0])
        for event in block_events:
            window = window.with_event(event)
        store = BotStorage(persisted_bot_data).aggregation_windows
        store.upsert_pending(window)

        await subscription.module_runtime.resume_pending_aggregations()

        assert store.pending() == []
        assert "aggregation_windows" not in initial_bot_data
        application.update_queue.put.assert_awaited_once()
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_process_event_log_does_not_advance_persisted_block():
    harness = _make_processing_harness(
        aggregation_group=None,
        event_names=frozenset(),
        bot_data={"block": 100},
    )

    await harness.runtime.handle_event(_make_event(block=200))

    assert harness.storage.state.block.value == 100


@pytest.mark.asyncio
async def test_handle_event_log_does_not_advance_persisted_block():
    sub = _make_notification_handler(event_messages_return=None)
    context = _make_context(block=100)

    await sub.handle_event_log(EventNotification.from_event(_make_event(block=200)), context)

    assert context.bot_storage.block.value == 100


@pytest.mark.asyncio
async def test_notification_log_is_emitted_only_with_sent_messages(caplog):
    sub = _make_notification_handler(event_messages_return=None)
    context = _make_context(block=100)

    with caplog.at_level("INFO", logger="sentinel.app.telegram_adapters"):
        await sub.handle_event_log(EventNotification.from_event(_make_event(block=200)), context)

    assert not any(
        record.getMessage().startswith("Notification handled on the block")
        for record in caplog.records
    )
    assert not any(record.getMessage().startswith("Messages sent:") for record in caplog.records)


@pytest.mark.asyncio
async def test_notification_log_includes_messages_sent_count(caplog):
    plan = NotificationPlan.broadcast("Test notification")
    sub = _make_notification_handler(event_messages_return=plan)
    context = SimpleNamespace(
        bot_storage=BotStorage(
            {
                "block": 100,
                "user_ids": {100},
                "group_ids": set(),
                "channel_ids": set(),
                "no_ids_to_chats": {},
            }
        ),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    with caplog.at_level("INFO", logger="sentinel.app.telegram_adapters"):
        await sub.handle_event_log(EventNotification.from_event(_make_event(block=200)), context)

    handled = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("Notification handled on the block")
    )
    assert "Messages sent: 1" in handled.getMessage()
    assert handled.event_name == "TestEvent"
    assert handled.block == 200
    assert handled.source_event_count == 1
    assert handled.sent_messages == 1
    context.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_per_chat_notification_sends_one_filtered_digest_per_chat():
    plan = NotificationPlan.per_chat(
        node_operator_ids={"1", "2"},
        render=lambda node_operator_ids: (",".join(sorted(node_operator_ids)),),
    )
    sub = _make_notification_handler(event_messages_return=plan)
    context = SimpleNamespace(
        bot_storage=BotStorage(
            {
                "user_ids": {100, 200, 300},
                "group_ids": set(),
                "channel_ids": set(),
                "no_ids_to_chats": {
                    "1": {100, 300},
                    "2": {200, 300},
                },
            }
        ),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    await sub.handle_event_log(EventNotification.from_event(_make_event(block=200)), context)

    messages_by_chat = {
        call.kwargs["chat_id"]: call.kwargs["text"]
        for call in context.bot.send_message.await_args_list
    }
    assert messages_by_chat == {
        100: "1",
        200: "2",
        300: "1,2",
    }


@pytest.mark.asyncio
async def test_per_chat_renderer_selects_specific_message_or_fallback():
    plan = NotificationPlan.per_chat(
        {"1", "2"},
        lambda node_operator_ids: ("specific",) if "1" in node_operator_ids else ("fallback",),
    )
    sub = _make_notification_handler(event_messages_return=plan)
    context = SimpleNamespace(
        bot_storage=BotStorage(
            {
                "user_ids": {100, 200, 300},
                "group_ids": set(),
                "channel_ids": set(),
                "no_ids_to_chats": {
                    "1": {100, 300},
                    "2": {200, 300},
                },
            }
        ),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    await sub.handle_event_log(EventNotification.from_event(_make_event(block=200)), context)

    messages_by_chat: dict[int, list[str]] = {}
    for call in context.bot.send_message.await_args_list:
        messages_by_chat.setdefault(call.kwargs["chat_id"], []).append(call.kwargs["text"])
    assert messages_by_chat == {
        100: ["specific"],
        200: ["fallback"],
        300: ["specific"],
    }


@pytest.mark.asyncio
async def test_handle_event_log_does_not_regress_persisted_block():
    sub = _make_notification_handler(event_messages_return=None)
    context = _make_context(block=500)

    await sub.handle_event_log(EventNotification.from_event(_make_event(block=300)), context)

    assert context.bot_storage.block.value == 500


@pytest.mark.asyncio
async def test_handle_event_log_does_not_advance_block_with_notification_plan():
    plan = NotificationPlan.broadcast("Test notification")
    sub = _make_notification_handler(event_messages_return=plan)
    context = _make_context(block=100)

    await sub.handle_event_log(EventNotification.from_event(_make_event(block=200)), context)

    assert context.bot_storage.block.value == 100


@pytest.mark.asyncio
async def test_handle_curated_release_broadcast_reaches_chats_without_subscriptions():
    plan = NotificationPlan.broadcast("Curated Module is live!")
    sub = _make_notification_handler(event_messages_return=plan)
    context = SimpleNamespace(
        bot_storage=BotStorage(
            {
                "user_ids": {100},
                "group_ids": {200},
                "channel_ids": {300},
                "no_ids_to_chats": {},
            }
        ),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    await sub.handle_event_log(EventNotification.from_event(_make_event(block=200)), context)

    assert {call.kwargs["chat_id"] for call in context.bot.send_message.await_args_list} == {
        100,
        200,
        300,
    }


@pytest.mark.asyncio
async def test_process_new_block_advances_persisted_block():
    harness = _make_processing_harness(
        aggregation_group=None,
        event_names=frozenset(),
        bot_data={"block": 100},
    )

    await harness.runtime.handle_block(Block(number=200))

    assert harness.storage.state.block.value == 200


@pytest.mark.asyncio
async def test_process_new_block_does_not_regress_persisted_block():
    harness = _make_processing_harness(
        aggregation_group=None,
        event_names=frozenset(),
        bot_data={"block": 500},
    )

    await harness.runtime.handle_block(Block(number=300))

    assert harness.storage.state.block.value == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_block", "live_head", "expected_checkpoint"),
    [(0, 25_586_956, 25_586_956), (25_586_960, 25_586_956, 25_586_960)],
)
async def test_checkpoint_current_head_does_not_regress_checkpoint(
    persisted_block: int,
    live_head: int,
    expected_checkpoint: int,
):
    supervisor = ModuleRuntimeSupervisor.__new__(ModuleRuntimeSupervisor)
    supervisor._storage = _FakeSubscriptionStorage({"block": persisted_block})
    supervisor.get_block_number = AsyncMock(return_value=live_head)

    result = await supervisor.checkpoint_current_head()

    assert result == live_head
    assert supervisor._storage.state.block.value == expected_checkpoint
