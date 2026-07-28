import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from hexbytes import HexBytes
import pytest
from web3.types import FilterParams


def test_event_readable_string():
    from sentinel.models import Event

    event = Event(
        event="TestEvent",
        args={"a": 1, "b": 2},
        block=100,
        tx=HexBytes("0x" + "00" * 32),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )
    s = event.readable()
    assert s.startswith("TestEvent(")
    assert "a=1" in s and "b=2" in s


def test_main_subscription_helpers_counts():
    from sentinel.handlers.utils import get_active_subscription_counts, get_subscription_totals
    from sentinel.app.storage import BotStorage

    bot_data = {
        "user_ids": {1},
        "group_ids": {2},
        "channel_ids": {3},
        "no_ids_to_chats": {
            "10": {1, 2},  # one user, one group
            "20": {2, 4},  # one group, one inactive
        },
    }

    storage = BotStorage(bot_data)
    counts = get_active_subscription_counts(storage)
    assert counts["10"]["total"] == 2
    assert counts["10"]["users"] == 1
    assert counts["10"]["groups"] == 1
    assert counts["10"]["channels"] == 0

    assert counts["20"]["total"] == 1
    assert counts["20"]["users"] == 0
    assert counts["20"]["groups"] == 1
    assert counts["20"]["channels"] == 0

    subscribers, node_operators = get_subscription_totals(storage)
    assert subscribers == 2  # chat ids 1 and 2 are active across all NOs
    assert node_operators == 2


def test_main_resolve_target_chats():
    from sentinel.handlers.utils import resolve_target_chats_for_node_operators
    from sentinel.app.storage import BotStorage

    bot_data = {
        "user_ids": {1},
        "group_ids": {2},
        "channel_ids": {3},
        "no_ids_to_chats": {
            "10": {1, 2},
            "20": {2, 4},
        },
    }
    storage = BotStorage(bot_data)
    targets = resolve_target_chats_for_node_operators(storage, {"10", "20"})
    assert targets == {1, 2}


def test_texts_manager_address_change_proposed_messages():
    from web3.constants import ADDRESS_ZERO
    from sentinel.modules.community.texts import node_operator_manager_address_change_proposed

    msg_revoked = node_operator_manager_address_change_proposed(ADDRESS_ZERO)
    assert "revoked" in msg_revoked

    msg_proposed = node_operator_manager_address_change_proposed("0x123")
    assert "New manager address proposed" in msg_proposed


def test_parse_distribution_log_supports_v1_v2_and_v3_report_shapes():
    from sentinel.modules.distribution import parse_distribution_log

    frame_1 = {
        "operators": {
            "42": {
                "validators": {
                    "100": {"strikes": 2},
                    "101": {"strikes": 0},
                }
            }
        }
    }
    frame_2 = {
        "operators": {
            777: {
                "validators": {
                    "900": {"strikes": 1},
                }
            }
        }
    }
    frame_3 = {
        "operators": {
            "888": {
                "validators": {
                    "901": {"strikes": 3},
                }
            }
        }
    }

    expected_operator_ids = {"42", "777", "888"}
    expected_strikes = {
        "42": [("100", 2)],
        "777": [("900", 1)],
        "888": [("901", 3)],
    }

    v1_summary = parse_distribution_log(frame_1)
    v2_summary = parse_distribution_log([frame_1, frame_2, frame_3])
    v3_summary = parse_distribution_log({"_ver": 1, "frames": [frame_1, frame_2, frame_3]})

    assert v1_summary.all_operator_ids == {"42"}
    assert v1_summary.strikes_per_operator == {"42": [("100", 2)]}
    assert v2_summary.all_operator_ids == expected_operator_ids
    assert v2_summary.strikes_per_operator == expected_strikes
    assert v3_summary.all_operator_ids == expected_operator_ids
    assert v3_summary.strikes_per_operator == expected_strikes


@patch.dict(
    os.environ,
    {
        "ETHERSCAN_URL": "https://etherscan.io",
        "BEACONCHAIN_URL": "https://beaconcha.in",
        "ADMIN_IDS": "1, 2 3,invalid,4",
        "BLOCK_BATCH_SIZE": "12345",
        "PROCESS_BLOCKS_REQUESTS_PER_SECOND": "3.5",
        "BLOCK_FROM": "789",
        "WEB3_SOCKET_PROVIDERS": "wss://example.invalid",
        "MODULE_ADDRESS": "0x0000000000000000000000000000000000000001",
    },
    clear=True,
)
def test_config_parsing_and_templates():
    from sentinel.config import load_config_from_env

    cfg = load_config_from_env()

    assert cfg.admin_ids == {1, 2, 3, 4}
    assert cfg.block_batch_size == 12345
    assert cfg.process_blocks_requests_per_second == 3.5
    assert cfg.block_from == 789
    assert cfg.etherscan_block_url_template == "https://etherscan.io/block/{}"
    assert cfg.etherscan_tx_url_template == "https://etherscan.io/tx/{}"
    assert cfg.beaconchain_url_template == "https://beaconcha.in/validator/{}"


@pytest.mark.asyncio
@patch.dict(
    os.environ,
    {
        "WEB3_SOCKET_PROVIDERS": "wss://example.invalid",
        "MODULE_ADDRESS": "0x0000000000000000000000000000000000000001",
        "PROCESS_BLOCKS_REQUESTS_PER_SECOND": "2",
    },
    clear=True,
)
async def test_process_blocks_rate_limit():
    from sentinel.config import load_config_from_env
    from sentinel.web3_event_log_reader import Web3EventLogReader

    cfg = load_config_from_env()
    reader = Web3EventLogReader(
        SimpleNamespace(),
        request_interval_seconds=1 / cfg.process_blocks_requests_per_second,
    )
    start = asyncio.get_running_loop().time()
    await reader.throttle()
    await reader.throttle()
    elapsed = asyncio.get_running_loop().time() - start
    assert elapsed >= 0.45


@pytest.mark.asyncio
async def test_event_log_reader_cancels_connect_when_stopped():
    from sentinel.web3_event_log_reader import Web3EventLogReader

    connect_started = asyncio.Event()
    connect_cancelled = asyncio.Event()

    async def connect() -> None:
        connect_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            connect_cancelled.set()
            raise

    stop_event = asyncio.Event()
    rpc_w3 = SimpleNamespace(
        provider=SimpleNamespace(
            is_connected=AsyncMock(return_value=False),
            connect=connect,
        )
    )
    reader = Web3EventLogReader(
        rpc_w3,
        request_interval_seconds=None,
        stop_event=stop_event,
    )

    fetch_task = asyncio.create_task(reader.fetch_events(start_block=1, end_block=1))
    await connect_started.wait()
    stop_event.set()

    assert await asyncio.wait_for(fetch_task, timeout=1) is None
    assert connect_cancelled.is_set()


@pytest.mark.asyncio
async def test_event_log_reader_cancels_active_rpc_when_stopped():
    from sentinel.web3_event_log_reader import Web3EventLogReader

    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()

    async def get_logs(_filter_params):
        request_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            request_cancelled.set()
            raise

    stop_event = asyncio.Event()
    rpc_w3 = SimpleNamespace(eth=SimpleNamespace(get_logs=get_logs))
    reader = Web3EventLogReader(
        rpc_w3,
        request_interval_seconds=None,
        stop_event=stop_event,
    )

    request_task = asyncio.create_task(
        reader.get_logs(
            w3=rpc_w3,
            filter_params=FilterParams(fromBlock=1, toBlock=1),
        )
    )
    await request_started.wait()
    stop_event.set()

    assert await asyncio.wait_for(request_task, timeout=1) is None
    assert request_cancelled.is_set()


@pytest.mark.asyncio
async def test_get_block_number_uses_persistent_provider(monkeypatch):
    from sentinel.rpc import Subscription
    from sentinel.app.health import HealthState

    class FakeAdapter:
        def event_sources(self):
            return ()

        def notifiable_events(self):
            return set()

        def side_effect_events(self):
            return set()

        def topic_abis(self):
            return ()

    main_w3 = SimpleNamespace(
        provider=SimpleNamespace(
            is_connected=AsyncMock(return_value=True),
            connect=AsyncMock(),
        ),
        eth=SimpleNamespace(get_block_number=AsyncMock(return_value=111)),
    )
    backfill_w3 = SimpleNamespace(
        provider=SimpleNamespace(
            is_connected=AsyncMock(return_value=True),
            connect=AsyncMock(),
        ),
        eth=SimpleNamespace(get_block_number=AsyncMock(return_value=222)),
    )

    monkeypatch.setattr(
        "sentinel.rpc.get_config",
        lambda: SimpleNamespace(process_blocks_requests_per_second=None),
    )
    subscription = Subscription(
        main_w3,
        health=HealthState(),
        module_adapter=FakeAdapter(),
        backfill_w3=backfill_w3,
    )

    latest = await subscription.get_block_number()

    assert latest == 111
    main_w3.eth.get_block_number.assert_awaited_once()
    backfill_w3.eth.get_block_number.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_blocks_uses_configured_event_log_reader():
    from sentinel.models import Block, Event
    from sentinel.rpc import Subscription

    class FakeAdapter:
        def event_sources(self):
            return ()

        def notifiable_events(self):
            return set()

        def side_effect_events(self):
            return set()

        def topic_abis(self):
            return ()

    monkeypatch_config = SimpleNamespace(
        process_blocks_requests_per_second=None,
        block_batch_size=1000,
    )
    from unittest.mock import patch
    from sentinel.app.health import HealthState

    with patch("sentinel.rpc.get_config", return_value=monkeypatch_config):
        subscription = Subscription(
            SimpleNamespace(),
            health=HealthState(),
            module_adapter=FakeAdapter(),
        )
    subscription.cfg = SimpleNamespace(block_batch_size=1000)
    subscription._shutdown_event = asyncio.Event()
    subscription._health = SimpleNamespace(mark_progress=lambda: None)
    event_consumer = SimpleNamespace(handle_event=AsyncMock())
    block_consumer = SimpleNamespace(handle_block=AsyncMock())
    subscription.add_event_consumer(event_consumer)
    subscription.add_block_consumer(block_consumer)
    event = Event(
        event="ScopedEvent",
        args={"keep": True},
        block=1,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000001",
        log_index=1,
        transaction_index=0,
    )

    class FakeEventLogReader:
        def __init__(self):
            self.fetch_events = AsyncMock(return_value=[event])
            self._w3 = SimpleNamespace(
                eth=SimpleNamespace(get_block_number=AsyncMock(return_value=2)),
            )

        async def connected_w3(self):
            return self._w3

    subscription._event_log_reader = FakeEventLogReader()

    await subscription.replay_blocks(1, 2)

    subscription._event_log_reader.fetch_events.assert_awaited_once_with(
        start_block=1,
        end_block=2,
    )
    event_consumer.handle_event.assert_awaited_once_with(event)
    block_consumer.handle_block.assert_awaited_once_with(Block(number=2))


@pytest.mark.asyncio
async def test_web3_event_log_reader_fetches_source_events_without_subscription(
    monkeypatch,
):
    from sentinel.models import Event
    from sentinel.modules.base import EventSource
    from sentinel.web3_event_log_reader import Web3EventLogReader

    topic = HexBytes("0x" + "02" * 32)
    w3 = SimpleNamespace(
        provider=SimpleNamespace(
            is_connected=AsyncMock(return_value=True),
            connect=AsyncMock(),
        ),
        eth=SimpleNamespace(
            get_logs=AsyncMock(
                return_value=[{"topics": [topic], "id": 1}, {"topics": [topic], "id": 2}]
            ),
        ),
    )
    event_sources = (
        EventSource(
            "scoped",
            "0x0000000000000000000000000000000000000002",
            frozenset({"ScopedEvent"}),
            lambda event: event.args["keep"],
        ),
    )
    abi_by_topics = {topic: {"name": "ScopedEvent"}}
    reader = Web3EventLogReader(
        w3,
        event_sources=event_sources,
        abi_by_topics=abi_by_topics,
        request_interval_seconds=None,
    )

    decoded_events = [
        Event(
            event="ScopedEvent",
            args={"keep": True},
            block=1,
            tx=HexBytes("0xdeadbeef"),
            address="0x0000000000000000000000000000000000000002",
            log_index=2,
            transaction_index=0,
        ),
        Event(
            event="ScopedEvent",
            args={"keep": True},
            block=1,
            tx=HexBytes("0xfeedbeef"),
            address="0x0000000000000000000000000000000000000002",
            log_index=1,
            transaction_index=0,
        ),
    ]
    monkeypatch.setattr(
        "sentinel.web3_event_log_reader.decode_event",
        lambda w3, event_abi, log: decoded_events.pop(0),
    )

    events = await reader.fetch_events(
        start_block=1,
        end_block=2,
    )

    filter_params = w3.eth.get_logs.await_args.args[0]
    assert filter_params["address"] == "0x0000000000000000000000000000000000000002"
    assert filter_params["topics"] == [topic]
    assert [event.log_index for event in events] == [1, 2]


@pytest.mark.asyncio
async def test_web3_event_log_reader_filters_validator_exit_requests_by_staking_module(
    monkeypatch,
):
    from sentinel.models import Event
    from sentinel.modules.base import EventSource
    from sentinel.web3_event_log_reader import Web3EventLogReader

    topic = HexBytes("0x" + "03" * 32)
    w3 = SimpleNamespace(
        provider=SimpleNamespace(
            is_connected=AsyncMock(return_value=True),
            connect=AsyncMock(),
        ),
        eth=SimpleNamespace(
            get_logs=AsyncMock(
                return_value=[{"topics": [topic], "id": 1}, {"topics": [topic], "id": 2}]
            ),
        ),
    )
    reader = Web3EventLogReader(
        w3,
        event_sources=(
            EventSource(
                "vebo",
                "0x0000000000000000000000000000000000000002",
                frozenset({"ValidatorExitRequest"}),
                lambda event: event.args["stakingModuleId"] == 5,
            ),
        ),
        abi_by_topics={topic: {"name": "ValidatorExitRequest"}},
        request_interval_seconds=None,
    )
    decoded_events = [
        Event(
            event="ValidatorExitRequest",
            args={"stakingModuleId": 1, "nodeOperatorId": 42},
            block=1,
            tx=HexBytes("0xdeadbeef"),
            address="0x0000000000000000000000000000000000000002",
            log_index=1,
            transaction_index=0,
        ),
        Event(
            event="ValidatorExitRequest",
            args={"stakingModuleId": 5, "nodeOperatorId": 42},
            block=1,
            tx=HexBytes("0xfeedbeef"),
            address="0x0000000000000000000000000000000000000002",
            log_index=2,
            transaction_index=0,
        ),
    ]
    monkeypatch.setattr(
        "sentinel.web3_event_log_reader.decode_event",
        lambda w3, event_abi, log: decoded_events.pop(0),
    )

    events = await reader.fetch_events(start_block=1, end_block=2)

    assert [event.args["stakingModuleId"] for event in events] == [5]


@pytest.mark.asyncio
async def test_web3_event_log_reader_skips_empty_source_event_filters():
    from sentinel.modules.base import EventSource
    from sentinel.web3_event_log_reader import Web3EventLogReader

    w3 = SimpleNamespace(
        provider=SimpleNamespace(
            is_connected=AsyncMock(return_value=True),
            connect=AsyncMock(),
        ),
        eth=SimpleNamespace(get_logs=AsyncMock(return_value=[])),
    )
    reader = Web3EventLogReader(
        w3,
        event_sources=(
            EventSource(
                "empty",
                "0x0000000000000000000000000000000000000002",
                frozenset(),
                None,
            ),
        ),
        abi_by_topics={},
        request_interval_seconds=None,
    )

    events = await reader.fetch_events(
        start_block=1,
        end_block=2,
    )

    assert events == []
    w3.eth.get_logs.assert_not_awaited()


def test_build_event_bindings_preserves_source_event_filter_intent():
    from sentinel.modules.base import EventSource
    from sentinel.web3_events import build_event_bindings

    class FakeAdapter:
        def event_sources(self):
            return (
                EventSource(
                    "all",
                    "0x0000000000000000000000000000000000000001",
                    None,
                    None,
                ),
                EventSource(
                    "empty",
                    "0x0000000000000000000000000000000000000002",
                    frozenset(),
                    None,
                ),
            )

        def notifiable_events(self):
            return {"Notifiable"}

        def side_effect_events(self):
            return set()

        def topic_abis(self):
            return ()

    bindings = build_event_bindings(FakeAdapter())

    assert bindings.event_sources[0].event_names is None
    assert bindings.event_sources[1].event_names == frozenset()


@pytest.mark.asyncio
async def test_web3_event_history_fetches_configured_reader_range():
    from sentinel.services.event_history import Web3EventHistory

    history = Web3EventHistory.__new__(Web3EventHistory)
    history._event_log_reader = SimpleNamespace(fetch_events=AsyncMock(return_value=[]))

    events = await history.fetch_events(1, 2)

    assert events == []
    history._event_log_reader.fetch_events.assert_awaited_once_with(
        start_block=1,
        end_block=2,
    )
