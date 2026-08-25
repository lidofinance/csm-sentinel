import asyncio
import pytest
from types import SimpleNamespace

from sentinel.chain import SharedChainConnection
from sentinel.config import clear_config, get_config, set_config
from sentinel.notifications import (
    BroadcastDelivery,
    NotificationPlan,
    OperatorMessagesDelivery,
    PerChatDelivery,
)
from sentinel.modules.community.texts import (
    bond_debt_covered,
    bond_debt_increased,
    custom_rewards_claimer_set,
    bond_lock_removed,
    fee_splits_set,
    key_allocated_balance_changed,
    target_validators_count_changed,
    validator_slashing_reported,
)
from hexbytes import HexBytes


def _broadcast(plan: NotificationPlan) -> str | None:
    delivery = plan.delivery
    if isinstance(delivery, BroadcastDelivery):
        return delivery.message
    return None


def _broadcast_operator_ids(plan: NotificationPlan) -> frozenset[str] | None:
    delivery = plan.delivery
    if isinstance(delivery, BroadcastDelivery):
        return delivery.operator_ids
    return None


def _operator_messages(plan: NotificationPlan):
    assert isinstance(plan.delivery, OperatorMessagesDelivery)
    return plan.delivery.messages


def _per_chat(plan: NotificationPlan) -> PerChatDelivery:
    assert isinstance(plan.delivery, PerChatDelivery)
    return plan.delivery


def _notification(event):
    from sentinel.models import EventNotification

    return EventNotification.from_event(event)


def _distribution_notification(event, distributed: int = 1):
    from sentinel.models import Event, EventNotification

    distributed_event = Event(
        event="ModuleFeeDistributed",
        args={"shares": distributed},
        block=event.block,
        tx=event.tx,
        address=event.address,
        log_index=event.log_index - 1,
        transaction_index=event.transaction_index,
    )
    return EventNotification(source_events=(distributed_event, event))


class _DummyConnectProvider:
    w3 = SimpleNamespace(to_hex=lambda value: "0x" + value.hex())

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class _FakeFetcher:
    def __init__(self, result=None, exc: Exception | None = None):
        self.result = result
        self.exc = exc
        self.calls: list[str] = []

    async def __call__(self, log_cid: str):
        self.calls.append(log_cid)
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeCall:
    def __init__(self, value, exc: Exception | None = None):
        self.value = value
        self.exc = exc
        self.calls: list[dict] = []

    async def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.value


class _FakeEventLogs:
    def __init__(self, logs: list[dict]):
        self.logs = logs
        self.calls: list[dict] = []

    async def get_logs(self, **kwargs):
        self.calls.append(kwargs)
        return self.logs


class _FakeOperatorWeightsCall:
    def __init__(
        self,
        node_operator_ids: list[int],
        weights_by_block: dict[int | None, dict[int, int]],
    ):
        self.node_operator_ids = node_operator_ids
        self.weights_by_block = weights_by_block
        self.calls: list[dict] = []

    async def call(self, **kwargs):
        self.calls.append(kwargs)
        block = kwargs.get("block_identifier")
        weights = self.weights_by_block.get(block, self.weights_by_block.get(None, {}))
        return [weights.get(node_operator_id, 1) for node_operator_id in self.node_operator_ids]


class _FakeMetaRegistry:
    def __init__(
        self,
        group_info=None,
        metadata_names: dict[int, str | None] | None = None,
        metadata_exc: Exception | None = None,
        operator_weights_by_block: dict[int | None, dict[int, int]] | None = None,
    ):
        self.metadata_names = metadata_names or {}
        self.metadata_exc = metadata_exc
        self.operator_weights_by_block = operator_weights_by_block or {}
        self.group_ids: list[int] = []
        self.metadata_ids: list[int] = []
        self.operator_weight_ids: list[list[int]] = []
        self.call = _FakeCall(group_info)
        self.metadata_calls: list[_FakeCall] = []
        self.operator_weight_calls: list[_FakeOperatorWeightsCall] = []
        self.functions = SimpleNamespace(
            getOperatorGroup=self.get_operator_group,
            getOperatorMetadata=self.get_operator_metadata,
            getOperatorWeights=self.get_operator_weights,
        )

    def get_operator_group(self, group_id: int):
        self.group_ids.append(group_id)
        return self.call

    def get_operator_metadata(self, node_operator_id: int):
        self.metadata_ids.append(node_operator_id)
        call = _FakeCall(
            {
                "name": self.metadata_names.get(node_operator_id),
                "description": "unused",
                "ownerEditsRestricted": False,
            },
            exc=self.metadata_exc,
        )
        self.metadata_calls.append(call)
        return call

    def get_operator_weights(self, node_operator_ids: list[int]):
        self.operator_weight_ids.append(node_operator_ids)
        call = _FakeOperatorWeightsCall(node_operator_ids, self.operator_weights_by_block)
        self.operator_weight_calls.append(call)
        return call


class _FakeCuratedModule:
    def __init__(
        self,
        operators_count: int,
        node_operators: dict[int, object] | None = None,
    ):
        self.operators_count_call = _FakeCall(operators_count)
        self.node_operators = node_operators or {}
        self.node_operator_calls: list[int] = []
        self.node_operator_call_objects: list[_FakeCall] = []
        self.functions = SimpleNamespace(
            getNodeOperatorsCount=self.get_node_operators_count,
            getNodeOperator=self.get_node_operator,
        )

    def get_node_operators_count(self):
        return self.operators_count_call

    def get_node_operator(self, node_operator_id: int):
        self.node_operator_calls.append(node_operator_id)
        call = _FakeCall(
            self.node_operators.get(
                node_operator_id,
                SimpleNamespace(totalAddedKeys=0, totalDepositedKeys=0),
            )
        )
        self.node_operator_call_objects.append(call)
        return call


class _FakeCuratedAccounting:
    def __init__(
        self,
        operator_curve_ids: dict[int, int] | None = None,
        fee_splits_by_operator: dict[int, list] | None = None,
    ):
        self.fee_splits_by_operator = fee_splits_by_operator or {}
        self.operator_curve_ids = operator_curve_ids
        self.curve_id_calls: list[int] = []
        self.curve_id_call_objects: list[_FakeCall] = []
        self.fee_split_calls: list[int] = []
        self.fee_split_call_objects: list[_FakeCall] = []
        self.functions = SimpleNamespace(
            getBondCurveId=self.get_bond_curve_id,
            getFeeSplits=self.get_fee_splits,
        )

    def get_bond_curve_id(self, node_operator_id: int):
        self.curve_id_calls.append(node_operator_id)
        call = _FakeCall(self.operator_curve_ids[node_operator_id])
        self.curve_id_call_objects.append(call)
        return call

    def get_fee_splits(self, node_operator_id: int):
        self.fee_split_calls.append(node_operator_id)
        call = _FakeCall(self.fee_splits_by_operator.get(node_operator_id, []))
        self.fee_split_call_objects.append(call)
        return call


class _FakeParametersRegistry:
    def __init__(self, allowed_exit_delays: dict[int, int]):
        self.allowed_exit_delays = allowed_exit_delays
        self.allowed_exit_delay_calls: list[int] = []
        self.allowed_exit_delay_call_objects: list[_FakeCall] = []
        self.functions = SimpleNamespace(getAllowedExitDelay=self.get_allowed_exit_delay)

    def get_allowed_exit_delay(self, curve_id: int):
        self.allowed_exit_delay_calls.append(curve_id)
        call = _FakeCall(self.allowed_exit_delays[curve_id])
        self.allowed_exit_delay_call_objects.append(call)
        return call


class _FakeCuratedAdapter:
    def __init__(self, *, contracts=None, notifiable_events: set[str] | None = None):
        self.chain = _DummyConnectProvider()
        self.addresses = SimpleNamespace(
            module="0x0000000000000000000000000000000000000001",
            accounting="0x0000000000000000000000000000000000000002",
            parameters_registry="0x0000000000000000000000000000000000000003",
        )
        self.contracts = contracts or SimpleNamespace(
            module=object(),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=_FakeMetaRegistry(),
        )
        self._notifiable_events = notifiable_events or set()
        self.remembered_labels: list[tuple[int, str | None]] = []
        self.staking_module_id_refreshes = 0

    def catalog_events(self):
        return self._notifiable_events

    def notifiable_events(self):
        return self._notifiable_events

    def remember_node_operator_label(self, operator_id: int, name: str | None) -> None:
        self.remembered_labels.append((operator_id, name))

    async def refresh_staking_module_id(self) -> None:
        self.staking_module_id_refreshes += 1


def _set_event_config():
    set_config(
        SimpleNamespace(
            beaconchain_url_template="https://beaconcha.in/validator/{}",
            etherscan_block_url_template="https://etherscan.io/block/{}",
            etherscan_tx_url_template="https://etherscan.io/tx/{}",
            module_ui_url="https://lido.fi",
        )
    )


def test_event_mappings_are_complete():
    from sentinel.main import _assert_event_mappings

    _assert_event_mappings()


def test_new_v3_message_templates_render_core_fields():
    assert "Bond debt increased" in bond_debt_increased("1 ether")
    assert "Debt increase: `1 ether`" in bond_debt_increased("1 ether")

    assert "Bond debt covered" in bond_debt_covered("2 ether")
    assert "Covered amount: `2 ether`" in bond_debt_covered("2 ether")

    assert "Custom rewards claimer changed" in custom_rewards_claimer_set("0x123")
    assert "Rewards claimer: `0x123`" in custom_rewards_claimer_set("0x123")

    assert "Custom rewards claimer removed" in custom_rewards_claimer_set(
        "0x0000000000000000000000000000000000000000"
    )

    assert "Bond lock removed" in bond_lock_removed()
    assert "Key balance increased" in key_allocated_balance_changed(7, "3 ether")
    assert "Key index: `7`" in key_allocated_balance_changed(7, "3 ether")
    assert "New allocated balance: `3 ether`" in key_allocated_balance_changed(7, "3 ether")


def test_curated_burn_and_lock_templates_render_eth_asset_amounts():
    from sentinel.modules.curated.texts import (
        bond_burned,
        bond_charged,
        bond_lock_changed,
        bond_lock_compensated,
    )

    assert "Burned amount: `1\\.4 ETH`" in bond_burned("1.4 ether")
    assert "Requested charge: `2 ETH`" in bond_charged("2 ether", "1.5 ether")
    assert "Charged amount: `1\\.5 ETH`" in bond_charged("2 ether", "1.5 ether")
    assert "Locked amount: `3 ETH`" in bond_lock_changed("3 ether", "Fri 01 Jan 2027")
    assert "Compensated amount: `0\\.5 ETH`" in bond_lock_compensated("0.5 ether")


def test_curated_operator_group_templates_render_compact_group_label():
    from sentinel.modules.curated.texts import (
        operator_group_cleared,
        operator_group_created,
        operator_group_updated,
    )

    sub_node_operators = [
        {
            "label": "#10 - Operator Ten",
            "share": 10000,
            "weightedShare": 10000,
        }
    ]

    created = operator_group_created(7, sub_node_operators, group_name="New Group")
    assert "Group: `7: New Group`" in created
    assert "Group id:" not in created
    assert "Group name:" not in created

    cleared = operator_group_cleared(7, ["#10 - Operator Ten"], group_name="")
    assert "Group: `7`" in cleared
    assert "Group: `7:" not in cleared
    assert "These Node Operators will no longer receive deposit allocation" in cleared

    renamed_from_empty = operator_group_updated(
        7,
        change_kind="renamed",
        old_group_name=None,
        new_group_name="New Group",
    )
    assert "Group: `7`" in renamed_from_empty
    assert "Group name set: `New Group`" in renamed_from_empty
    assert "<empty" not in renamed_from_empty
    assert "Node Operator:" not in renamed_from_empty

    renamed_to_empty = operator_group_updated(
        7,
        change_kind="renamed",
        old_group_name="Old Group",
        new_group_name=None,
    )
    assert "Group: `7`" in renamed_to_empty
    assert "Group name cleared: `Old Group`" in renamed_to_empty

    renamed = operator_group_updated(
        7,
        change_kind="renamed",
        old_group_name="Old Group",
        new_group_name="New Group",
    )
    assert "Group: `7`" in renamed
    assert "Group renamed: `Old Group` \\-\\> `New Group`" in renamed


def test_validator_slashing_reported_template_renders_pubkey_link():
    message = validator_slashing_reported(
        "0x1234",
        "https://beaconcha.in/validator/0x1234",
        7,
    )

    assert "Validator slashing reported" in message
    assert "[0x1234](https://beaconcha.in/validator/0x1234)" in message
    assert "Key index: `7`" in message


def test_fee_splits_set_template_renders_fee_split_entries():
    set_config(SimpleNamespace(module_ui_url="https://csm.lido.fi"))
    try:
        message = fee_splits_set(
            [
                {"recipient": "0x111", "share": 7000},
                SimpleNamespace(recipient="0x222", share=3000),
            ]
        )
    finally:
        clear_config()

    assert "Fee splits set" in message
    assert "70%: `0x111`" in message
    assert "30%: `0x222`" in message
    assert "[CSM UI](https://csm.lido.fi)" in message


def test_fee_splits_set_template_renders_previous_and_current_entries():
    set_config(SimpleNamespace(module_ui_url="https://csm.lido.fi"))
    try:
        message = fee_splits_set(
            [{"recipient": "0x111", "share": 8000}],
            [{"recipient": "0x111", "share": 7000}],
        )
    finally:
        clear_config()

    assert "Fee splits changed" in message
    assert "Previous fee splits" in message
    assert "70%: `0x111`" in message
    assert "Fee splits" in message
    assert "80%: `0x111`" in message


def test_fee_splits_set_template_renders_removed_state():
    set_config(SimpleNamespace(module_ui_url="https://csm.lido.fi"))
    try:
        message = fee_splits_set([], [{"recipient": "0x111", "share": 7000}])
    finally:
        clear_config()

    assert "Fee splits removed" in message
    assert "Previous fee splits" in message
    assert "70%: `0x111`" in message


@pytest.mark.asyncio
async def test_fee_splits_set_reads_previous_fee_splits_at_previous_block():
    from sentinel.models import Event
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    accounting = _FakeCuratedAccounting(
        fee_splits_by_operator={42: [{"recipient": "0x111", "share": 7000}]}
    )
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=object(),
            accounting=accounting,
            parameters_registry=object(),
            meta_registry=_FakeMetaRegistry(),
        ),
        notifiable_events={"FeeSplitsSet"},
    )
    event_messages = CommunityEventMessages(adapter, distribution_log_fetcher=_FakeFetcher({}))
    event = Event(
        event="FeeSplitsSet",
        args={
            "nodeOperatorId": 42,
            "feeSplits": [{"recipient": "0x222", "share": 3000}],
        },
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert isinstance(plan, NotificationPlan)
    assert accounting.fee_split_calls == [42]
    assert [call.calls for call in accounting.fee_split_call_objects] == [
        [{"block_identifier": 122}]
    ]
    message = _broadcast(plan)
    assert message is not None
    assert "Fee splits changed" in message
    assert "Previous fee splits" in message
    assert "70%: `0x111`" in message
    assert "30%: `0x222`" in message


def test_limit_set_mode_1():
    result = target_validators_count_changed(0, 0, 1, 10, 15)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to 10\."
        "\n"
        r"5 keys above the limit will be requested to exit first\."
    )
    assert result == expected


def test_limit_set_mode_2():
    result = target_validators_count_changed(0, 0, 2, 180, 200)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to 180\."
        "\n"
        r"20 keys above the limit will be requested to exit immediately\."
    )
    assert result == expected


def test_limit_set_above_active_validators_count():
    result = target_validators_count_changed(0, 0, 2, 180, 170)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to 180\."
        "\n"
        r"0 keys above the limit will be requested to exit immediately\."
    )
    assert result == expected


def test_limit_set_mode_2_from_1():
    result = target_validators_count_changed(1, 5, 2, 10, 15)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to 10\."
        "\n"
        r"5 keys above the limit will be requested to exit immediately\."
    )
    assert result == expected


def test_limit_set_mode_1_from_2():
    result = target_validators_count_changed(2, 5, 1, 10, 15)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to 10\."
        "\n"
        r"5 keys above the limit will be requested to exit first\."
    )
    assert result == expected


def test_limit_decreased_mode_1():
    result = target_validators_count_changed(1, 10, 1, 3, 10)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been decreased from 10 to 3\."
        "\n"
        r"7 keys above the limit will be requested to exit first\."
    )
    assert result == expected


def test_limit_decreased_mode_2():
    result = target_validators_count_changed(2, 10, 2, 3, 10)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been decreased from 10 to 3\."
        "\n"
        r"7 keys above the limit will be requested to exit immediately\."
    )
    assert result == expected


def test_limit_to_zero_exit_first():
    result = target_validators_count_changed(1, 10, 1, 0, 10)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to zero\."
        "\n"
        r"10 keys will be requested to exit first\."
    )
    assert result == expected


def test_limit_to_zero_exit_immediately():
    result = target_validators_count_changed(2, 10, 2, 0, 10)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to zero\."
        "\n"
        r"10 keys will be requested to exit immediately\."
    )
    assert result == expected


def test_limit_to_zero_exit_first_no_previous_limit():
    result = target_validators_count_changed(0, 0, 1, 0, 10)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to zero\."
        "\n"
        r"10 keys will be requested to exit first\."
    )
    assert result == expected


def test_limit_to_zero_exit_immediately_no_previous_limit():
    result = target_validators_count_changed(0, 0, 2, 0, 10)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to zero\."
        "\n"
        r"10 keys will be requested to exit immediately\."
    )
    assert result == expected


def test_limit_unset_mode_zero():
    result = target_validators_count_changed(1, 10, 0, 0, 10)
    expected = (
        "🚨 *Target validators count changed*\n\n"
        r"The limit has been set to zero\. No keys will be requested to exit\."
    )
    assert result == expected


def test_forced_exit_target_limit_unset():
    result = target_validators_count_changed(2, 180, 0, 0, 180)
    expected = (
        "✅ *Unsetting forced exit target limit*\n\n"
        r"The forced target validator limit of `180` has been removed\."
        "\n"
        r"No additional validators will be requested to exit\."
    )
    assert result == expected


@pytest.mark.asyncio
async def test_key_removal_charge_is_multiplied_by_removed_keys_count():
    from sentinel.models import Event
    from sentinel.modules.community.events import CommunityEventMessages

    tx = HexBytes("0xdeadbeef")
    removed_key_logs = _FakeEventLogs(
        [
            {"transactionIndex": 5, "args": {"nodeOperatorId": 42}},
            {"transactionIndex": 5, "args": {"nodeOperatorId": 42}},
            {"transactionIndex": 5, "args": {"nodeOperatorId": 42}},
            {"transactionIndex": 8, "args": {"nodeOperatorId": 42}},
            {"transactionIndex": 5, "args": {"nodeOperatorId": 7}},
        ]
    )
    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.module = SimpleNamespace(
        events=SimpleNamespace(SigningKeyRemoved=lambda: removed_key_logs)
    )
    event_messages.accounting = SimpleNamespace(
        functions=SimpleNamespace(getBondCurveId=lambda _node_operator_id: _FakeCall(3))
    )
    event_messages.parametersRegistry = SimpleNamespace(
        functions=SimpleNamespace(
            getKeyRemovalCharge=lambda _curve_id: _FakeCall(20_000_000_000_000_000)
        )
    )

    async def notification_footer(_event):
        return ""

    event_messages.notification_footer = notification_footer
    event = Event(
        event="KeyRemovalChargeApplied",
        args={"nodeOperatorId": 42},
        block=123,
        tx=tx,
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=5,
    )

    message = await CommunityEventMessages.key_removal_charge_applied(
        event_messages, _notification(event)
    )

    assert "Amount of charge: `0\\.06 ether`" in message
    assert removed_key_logs.calls == [{"from_block": 123, "to_block": 123}]


@pytest.mark.asyncio
async def test_fetch_distribution_log_success():
    from sentinel.modules.community.events import CommunityEventMessages

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    fetcher = _FakeFetcher(result={"operators": {"123": {}}})
    event_messages._distribution_log_fetcher = fetcher

    data = await event_messages._fetch_distribution_log("QmCID")

    assert data == {"operators": {"123": {}}}
    assert fetcher.calls == ["QmCID"]


@pytest.mark.asyncio
async def test_fetch_distribution_log_caches():
    from sentinel.modules.community.events import CommunityEventMessages

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    fetcher = _FakeFetcher(result={"operators": {}})
    event_messages._distribution_log_fetcher = fetcher

    await event_messages._fetch_distribution_log("QmCID")
    await event_messages._fetch_distribution_log("QmCID")

    assert fetcher.calls == ["QmCID"]


@pytest.mark.asyncio
async def test_fetch_distribution_log_handles_error():
    from sentinel.modules.community.events import CommunityEventMessages

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages._distribution_log_fetcher = _FakeFetcher(exc=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await event_messages._fetch_distribution_log("QmCID")


@pytest.mark.asyncio
async def test_fetch_distribution_log_requires_cid():
    from sentinel.modules.community.events import CommunityEventMessages

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)

    with pytest.raises(ValueError):
        await event_messages._fetch_distribution_log(None)


@pytest.mark.asyncio
async def test_fetch_distribution_log_handles_timeout():
    from sentinel.modules.community.events import CommunityEventMessages

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages._distribution_log_fetcher = _FakeFetcher(exc=asyncio.TimeoutError("timeout"))

    with pytest.raises(asyncio.TimeoutError):
        await event_messages._fetch_distribution_log("QmCID")


@pytest.mark.asyncio
async def test_distribution_log_updated_produces_strike_notifications():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.notifications import NotificationPlan
    from sentinel.models import Event
    from sentinel.modules.community import texts

    set_config(
        SimpleNamespace(
            etherscan_tx_url_template="https://etherscan.io/tx/{}",
            module_ui_url="https://csm.lido.fi",
        )
    )
    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.cfg = get_config()

    payload = [
        {
            "operators": {
                "42": {
                    "validators": {
                        "123": {"strikes": 0},
                        "124": {"strikes": 2},
                    }
                },
                "777": {"validators": {"900": {"strikes": 0}}},
            }
        }
    ]

    fetch_calls: list[str | None] = []

    async def fake_fetch(log_cid):
        fetch_calls.append(log_cid)
        return payload

    event_messages._distribution_log_fetcher = fake_fetch

    event = Event(
        event="DistributionLogUpdated",
        args={"logCid": "cid123"},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=2,
        transaction_index=0,
    )

    plan = await CommunityEventMessages.distribution_log_updated(
        event_messages, _distribution_notification(event)
    )

    assert isinstance(plan, NotificationPlan)
    delivery = _per_chat(plan)
    assert delivery.operator_ids == {"42", "777"}

    expected_base = texts.distribution_data_updated()
    expected_foot = await event_messages.event_footer(event)
    fallback_message = f"{expected_base}{expected_foot}"
    (operator_message,) = delivery.render(frozenset({"42"}))
    assert expected_base in operator_message
    assert "⚠️" in operator_message
    assert "Validators with strikes: `1`" in operator_message
    assert operator_message.endswith(expected_foot)

    assert delivery.render(frozenset({"777"})) == (fallback_message,)
    assert delivery.render(frozenset({"42", "777"})) == (operator_message,)
    assert fetch_calls == ["cid123"]


@pytest.mark.asyncio
async def test_distribution_log_updated_handles_empty_payload():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.notifications import NotificationPlan
    from sentinel.models import Event
    from sentinel.modules.community import texts

    set_config(
        SimpleNamespace(
            etherscan_tx_url_template="https://etherscan.io/tx/{}",
            module_ui_url="https://csm.lido.fi",
        )
    )
    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.cfg = get_config()
    event_messages._distribution_log_fetcher = _FakeFetcher(result={})

    event = Event(
        event="DistributionLogUpdated",
        args={"logCid": "cid123"},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=2,
        transaction_index=0,
    )

    plan = await CommunityEventMessages.distribution_log_updated(
        event_messages, _distribution_notification(event)
    )

    assert isinstance(plan, NotificationPlan)
    assert _broadcast_operator_ids(plan) is None
    expected_base = texts.distribution_data_updated()
    expected_foot = await event_messages.event_footer(event)
    assert _broadcast(plan) == f"{expected_base}{expected_foot}"


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name", ["community", "curated"])
async def test_distribution_log_updated_skips_zero_distributed_rewards(module_name):
    from sentinel.models import Event
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.modules.curated.events import CuratedEventMessages

    _set_event_config()
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=object(),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=_FakeMetaRegistry(),
        ),
        notifiable_events={"DistributionLogUpdated"},
    )
    fetcher = _FakeFetcher(result={})
    event_messages_class = (
        CommunityEventMessages if module_name == "community" else CuratedEventMessages
    )
    event_messages = event_messages_class(adapter, distribution_log_fetcher=fetcher)
    event = Event(
        event="DistributionLogUpdated",
        args={"logCid": "cid123"},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=4,
        transaction_index=4,
    )

    plan = await event_messages.get_notification_plan(
        _distribution_notification(event, distributed=0)
    )

    assert plan is None
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_curated_distribution_log_updated_enriches_strike_operator_name():
    from sentinel.models import Event
    from sentinel.modules.curated import texts
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    meta_registry = _FakeMetaRegistry(metadata_names={42: "Operator Forty Two"})
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=_FakeCuratedModule(
                0, {42: SimpleNamespace(totalAddedKeys=0, totalDepositedKeys=1)}
            ),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=meta_registry,
        ),
        notifiable_events={"DistributionLogUpdated"},
    )
    payload = {
        "operators": {
            "42": {"validators": {"124": {"strikes": 2}}},
            "777": {"validators": {"900": {"strikes": 0}}},
        }
    }
    event_messages = CuratedEventMessages(
        adapter, distribution_log_fetcher=_FakeFetcher(result=payload)
    )
    event = Event(
        event="DistributionLogUpdated",
        args={"logCid": "cid123"},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=2,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_distribution_notification(event))

    assert isinstance(plan, NotificationPlan)
    expected_base = texts.distribution_data_updated()
    expected_foot = await event_messages.event_footer(event)
    fallback_message = f"{expected_base}{expected_foot}"
    delivery = _per_chat(plan)
    assert delivery.operator_ids == {"42", "777"}
    (operator_message,) = delivery.render(frozenset({"42"}))
    assert "Node Operator: `\\#42 \\- Operator Forty Two`" in operator_message
    assert "Operator ID" not in operator_message
    assert "Validators with strikes: `1`" in operator_message
    assert delivery.render(frozenset({"777"})) == (fallback_message,)
    assert delivery.render(frozenset({"42", "777"})) == (operator_message,)
    assert meta_registry.metadata_ids == [42]
    assert meta_registry.metadata_calls[0].calls == [{"block_identifier": 123}]


@pytest.mark.asyncio
async def test_get_notification_plan_skips_disallowed_event():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.models import Event

    class DummyAdapter:
        def catalog_events(self):
            return set()

        def notifiable_events(self):
            return set()

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.module_adapter = DummyAdapter()

    event = Event(
        event="DepositedSigningKeysCountChanged",
        args={"nodeOperatorId": 321, "depositedKeysCount": 1},
        block=1,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await CommunityEventMessages.get_notification_plan(event_messages, _notification(event))

    assert plan is None


@pytest.mark.asyncio
async def test_get_notification_plan_sets_node_operator_target():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.notifications import NotificationPlan
    from sentinel.models import Event

    class DummyAdapter:
        def catalog_events(self):
            return {"DepositedSigningKeysCountChanged"}

        def notifiable_events(self):
            return {"DepositedSigningKeysCountChanged"}

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.chain = _DummyConnectProvider()
    event_messages.cfg = SimpleNamespace(
        etherscan_tx_url_template="https://etherscan.io/tx/{}",
        etherscan_block_url_template="https://etherscan.io/block/{}",
    )
    event_messages.module_adapter = DummyAdapter()
    event_messages.module = _FakeCuratedModule(
        0, {321: SimpleNamespace(totalAddedKeys=0, totalDepositedKeys=0)}
    )

    event = Event(
        event="DepositedSigningKeysCountChanged",
        args={"nodeOperatorId": 321, "depositedKeysCount": 1},
        block=1,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await CommunityEventMessages.get_notification_plan(event_messages, _notification(event))

    assert isinstance(plan, NotificationPlan)
    delivery = _per_chat(plan)
    assert delivery.operator_ids == {"321"}
    assert "Deposit digest" in delivery.render(frozenset({"321"}))[0]


def test_curated_event_messages_reconfigure_extends_base_bindings():
    from sentinel.modules.curated.events import CuratedEventMessages

    _set_event_config()
    contracts = SimpleNamespace(
        module=object(),
        accounting=object(),
        parameters_registry=object(),
        meta_registry=object(),
    )
    adapter = _FakeCuratedAdapter(contracts=contracts)

    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))

    assert event_messages.module_adapter is adapter
    assert event_messages.chain is adapter.chain
    assert event_messages.module_address == adapter.addresses.module
    assert event_messages.accounting_address == adapter.addresses.accounting
    assert event_messages.parameters_registry_address == adapter.addresses.parameters_registry
    assert event_messages.module is contracts.module
    assert event_messages.accounting is contracts.accounting
    assert event_messages.parametersRegistry is contracts.parameters_registry
    assert event_messages.meta_registry is contracts.meta_registry


@pytest.mark.asyncio
async def test_curated_get_notification_plan_uses_inherited_base_handler():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    module = _FakeCuratedModule(0, {42: SimpleNamespace(totalAddedKeys=0, totalDepositedKeys=1)})
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=module,
            accounting=object(),
            parameters_registry=object(),
            meta_registry=_FakeMetaRegistry(metadata_names={42: "Galaxy - PTO"}),
        ),
        notifiable_events={"DepositedSigningKeysCountChanged"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="DepositedSigningKeysCountChanged",
        args={"nodeOperatorId": 42, "depositedKeysCount": 3},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert isinstance(plan, NotificationPlan)
    delivery = _per_chat(plan)
    assert delivery.operator_ids == {"42"}
    (message,) = delivery.render(frozenset({"42"}))
    assert message == (
        "🤩 *Deposit digest*\n\n"
        "Node Operators:\n"
        "\\- \\#42 \\- Galaxy \\- PTO: `1 \\-\\> 3`\n\n"
        "Block: [123](https://etherscan.io/block/123)"
    )
    assert module.node_operator_calls == [42]
    assert module.node_operator_call_objects[0].calls == [{"block_identifier": 122}]


@pytest.mark.asyncio
async def test_curated_resumed_builds_temporary_release_broadcast():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    adapter = _FakeCuratedAdapter(notifiable_events={"Resumed"})
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="Resumed",
        args={},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address=adapter.addresses.module,
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert adapter.staking_module_id_refreshes == 1
    assert isinstance(plan, NotificationPlan)
    assert _broadcast_operator_ids(plan) is None
    message = _broadcast(plan)
    assert message is not None
    assert "Curated Module is live" in message
    assert "Transaction" in message


@pytest.mark.asyncio
async def test_curated_footer_enriches_node_operator_name_and_caches_metadata():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages

    _set_event_config()
    meta_registry = _FakeMetaRegistry(metadata_names={42: "Lido Test Operator"})
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=_FakeCuratedModule(
                0, {42: SimpleNamespace(totalAddedKeys=0, totalDepositedKeys=1)}
            ),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=meta_registry,
        ),
        notifiable_events={"TotalSigningKeysCountChanged"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="TotalSigningKeysCountChanged",
        args={"nodeOperatorId": 42, "totalKeysCount": 3},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    first_plan = await event_messages.get_notification_plan(_notification(event))
    second_plan = await event_messages.get_notification_plan(_notification(event))

    first_message = _broadcast(first_plan)
    assert first_message is not None
    assert "Node Operator: \\#42 \\- Lido Test Operator" in first_message
    assert "description" not in first_message.lower()
    assert _broadcast(second_plan) == first_message
    assert meta_registry.metadata_ids == [42]
    assert meta_registry.metadata_calls[0].calls == [{"block_identifier": 123}]


@pytest.mark.asyncio
async def test_curated_footer_falls_back_when_metadata_fetch_fails():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages

    _set_event_config()
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=_FakeCuratedModule(
                0, {42: SimpleNamespace(totalAddedKeys=0, totalDepositedKeys=1)}
            ),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=_FakeMetaRegistry(metadata_exc=TimeoutError("metadata unavailable")),
        ),
        notifiable_events={"TotalSigningKeysCountChanged"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="TotalSigningKeysCountChanged",
        args={"nodeOperatorId": 42, "totalKeysCount": 3},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    message = _broadcast(plan)
    assert message is not None
    assert "nodeOperatorId: 42" in message
    assert "Node Operator: \\#42" not in message


@pytest.mark.asyncio
async def test_curated_bond_deposited_eth_formats_tx_only_message():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    adapter = _FakeCuratedAdapter(notifiable_events={"BondDepositedETH"})
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="BondDepositedETH",
        args={"from": "0x0000000000000000000000000000000000000abc", "amount": 10**18},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert isinstance(plan, NotificationPlan)
    assert _broadcast_operator_ids(plan) is None
    message = _broadcast(plan)
    assert message is not None
    assert "Bond deposited" in message
    assert "Asset:" not in message
    assert "Amount: `1 ETH`" in message
    assert "nodeOperatorId" not in message
    assert "[Transaction](https://etherscan.io/tx/0xdeadbeef)" in message


@pytest.mark.asyncio
async def test_curated_operator_group_created_targets_all_added_sub_node_operators():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    meta_registry = _FakeMetaRegistry(
        metadata_names={10: "Operator Ten", 11: "Operator Eleven"},
        operator_weights_by_block={123: {10: 1, 11: 4}},
    )
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=object(),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=meta_registry,
        ),
        notifiable_events={"OperatorGroupCreated"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="OperatorGroupCreated",
        args={
            "groupId": 7,
            "groupInfo": {
                "name": "Test Group",
                "subNodeOperators": [
                    {"nodeOperatorId": 10, "share": 4},
                    {"nodeOperatorId": 11, "share": 1},
                ],
            },
        },
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert isinstance(plan, NotificationPlan)
    assert _broadcast_operator_ids(plan) == {"10", "11"}
    message = _broadcast(plan)
    assert message is not None
    assert "Operator group created" in message
    assert "Group: `7: Test Group`" in message
    assert "Added Node Operators" in message
    assert "Operator Ten" in message
    assert "Weighted share: 50% \\(group share: 0\\.04%\\)" in message
    assert "Effective weight" not in message
    assert "Operator Eleven" in message
    assert "Weighted share: 50% \\(group share: 0\\.01%\\)" in message
    assert meta_registry.metadata_ids == [10, 11]
    assert [call.calls for call in meta_registry.metadata_calls] == [
        [{"block_identifier": 123}],
        [{"block_identifier": 123}],
    ]
    assert meta_registry.operator_weight_ids == [[10, 11]]
    assert [call.calls for call in meta_registry.operator_weight_calls] == [
        [{"block_identifier": 123}]
    ]


@pytest.mark.asyncio
async def test_curated_operator_group_updated_targets_only_changed_sub_node_operators():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    meta_registry = _FakeMetaRegistry(
        {
            "name": "Test Group",
            "subNodeOperators": [
                {"nodeOperatorId": 10, "share": 4},
                {"nodeOperatorId": 11, "share": 1},
            ],
        },
        metadata_names={10: "Operator Ten", 11: "Operator Eleven", 12: "Operator Twelve"},
        operator_weights_by_block={
            122: {10: 1, 11: 4},
            123: {10: 1, 12: 5},
        },
    )
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=object(),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=meta_registry,
        ),
        notifiable_events={"OperatorGroupUpdated"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="OperatorGroupUpdated",
        args={
            "groupId": 7,
            "groupInfo": {
                "name": "Test Group",
                "subNodeOperators": [
                    {"nodeOperatorId": 10, "share": 5},
                    {"nodeOperatorId": 12, "share": 2},
                ],
            },
        },
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert isinstance(plan, NotificationPlan)
    assert meta_registry.group_ids == [7]
    assert meta_registry.call.calls == [{"block_identifier": 122}]
    operator_messages = _operator_messages(plan)
    assert set(operator_messages) == {"10", "11", "12"}
    assert "Changes:" in operator_messages["10"]
    assert "Updated \\#10 \\- Operator Ten" in operator_messages["10"]
    assert "Share: `0\\.04% \\-\\> 0\\.05%`" in operator_messages["10"]
    assert "Effective allocation share: `50% \\-\\> 33\\.33%`" in operator_messages["10"]
    assert "Effective weight" not in operator_messages["10"]
    assert "Node Operator:" not in operator_messages["10"]
    assert "Removed \\#11 \\- Operator Eleven" in operator_messages["11"]
    assert "Previous Share: `0\\.01%`" in operator_messages["11"]
    assert "Previous Effective allocation share: `50%`" in operator_messages["11"]
    assert "Node Operator:" not in operator_messages["11"]
    assert "Added \\#12 \\- Operator Twelve" in operator_messages["12"]
    assert "Share: `0\\.02%`" in operator_messages["12"]
    assert "Effective allocation share: `66\\.66%`" in operator_messages["12"]
    assert "Effective weight" not in operator_messages["12"]
    assert "Node Operator:" not in operator_messages["12"]
    assert meta_registry.operator_weight_ids == [[10, 11], [10, 12]]
    assert [call.calls for call in meta_registry.operator_weight_calls] == [
        [{"block_identifier": 122}],
        [{"block_identifier": 123}],
    ]


@pytest.mark.asyncio
async def test_curated_operator_group_batch_renders_net_diff_for_clear_and_create_block():
    from sentinel.models import Event
    from sentinel.modules.aggregation import OperatorGroupChangeAggregator
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    meta_registry = _FakeMetaRegistry(
        {
            "name": "Old Group",
            "subNodeOperators": [
                {"nodeOperatorId": 10, "share": 4},
                {"nodeOperatorId": 11, "share": 1},
            ],
        },
        metadata_names={10: "Operator Ten", 11: "Operator Eleven", 12: "Operator Twelve"},
        operator_weights_by_block={
            122: {10: 1, 11: 4},
            123: {10: 1, 12: 5},
        },
    )
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=object(),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=meta_registry,
        ),
        notifiable_events={"OperatorGroupUpdated"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    block_events = [
        Event(
            event="OperatorGroupCleared",
            args={"groupId": 7},
            block=123,
            tx=HexBytes("0xdeadbeef"),
            address="0x0000000000000000000000000000000000000000",
            log_index=1,
            transaction_index=0,
        ),
        Event(
            event="NodeOperatorEffectiveWeightChanged",
            args={"nodeOperatorId": 10, "oldWeight": 1, "newWeight": 2},
            block=123,
            tx=HexBytes("0xdeadbeef"),
            address="0x0000000000000000000000000000000000000000",
            log_index=2,
            transaction_index=0,
        ),
        Event(
            event="OperatorGroupCreated",
            args={
                "groupId": 7,
                "groupInfo": {
                    "name": "New Group",
                    "subNodeOperators": [
                        {"nodeOperatorId": 10, "share": 5},
                        {"nodeOperatorId": 12, "share": 2},
                    ],
                },
            },
            block=123,
            tx=HexBytes("0xdeadbeef"),
            address="0x0000000000000000000000000000000000000000",
            log_index=3,
            transaction_index=0,
        ),
    ]

    notifications = OperatorGroupChangeAggregator().aggregate(block_events)
    assert len(notifications) == 1
    assert notifications[0].event == "OperatorGroupUpdated"
    assert [event.event for event in notifications[0].source_events] == [
        "OperatorGroupCleared",
        "OperatorGroupCreated",
        "OperatorGroupUpdated",
    ]

    plan = await event_messages.get_notification_plan(notifications[0])

    assert isinstance(plan, NotificationPlan)
    operator_messages = _operator_messages(plan)
    assert set(operator_messages) == {"10", "11", "12"}
    assert "Group renamed: `Old Group` \\-\\> `New Group`" in operator_messages["10"]
    assert "Updated \\#10 \\- Operator Ten" in operator_messages["10"]
    assert "Share: `0\\.04% \\-\\> 0\\.05%`" in operator_messages["10"]
    assert "Effective allocation share: `50% \\-\\> 33\\.33%`" in operator_messages["10"]
    assert "Removed \\#11 \\- Operator Eleven" in operator_messages["11"]
    assert "Previous Effective allocation share: `50%`" in operator_messages["11"]
    assert "Added \\#12 \\- Operator Twelve" in operator_messages["12"]
    assert "Share: `0\\.02%`" in operator_messages["12"]
    assert "Effective allocation share: `66\\.66%`" in operator_messages["12"]
    assert "Operator effective weight changed" not in "".join(operator_messages.values())


@pytest.mark.asyncio
async def test_curated_operator_group_updated_notifies_group_name_change():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    meta_registry = _FakeMetaRegistry(
        {
            "name": "Old Group",
            "subNodeOperators": [
                {"nodeOperatorId": 10, "share": 4},
                {"nodeOperatorId": 11, "share": 1},
            ],
        },
        metadata_names={10: "Operator Ten", 11: "Operator Eleven"},
        operator_weights_by_block={123: {10: 1, 11: 4}},
    )
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=object(),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=meta_registry,
        ),
        notifiable_events={"OperatorGroupUpdated"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="OperatorGroupUpdated",
        args={
            "groupId": 7,
            "groupInfo": {
                "name": "New Group",
                "subNodeOperators": [
                    {"nodeOperatorId": 10, "share": 4},
                    {"nodeOperatorId": 11, "share": 1},
                ],
            },
        },
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert isinstance(plan, NotificationPlan)
    assert meta_registry.group_ids == [7]
    assert meta_registry.call.calls == [{"block_identifier": 122}]
    assert _broadcast_operator_ids(plan) == {"10", "11"}
    message = _broadcast(plan)
    assert message is not None
    assert "Operator group updated" in message
    assert "Group: `7`" in message
    assert "Group renamed: `Old Group` \\-\\> `New Group`" in message
    assert "Node Operator:" not in message
    assert meta_registry.operator_weight_ids == []


@pytest.mark.asyncio
async def test_curated_operator_group_updated_notifies_unchanged_operators_on_group_rename():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    meta_registry = _FakeMetaRegistry(
        {
            "name": "Old Group",
            "subNodeOperators": [
                {"nodeOperatorId": 10, "share": 4},
                {"nodeOperatorId": 11, "share": 1},
            ],
        },
        metadata_names={
            10: "Operator Ten",
            11: "Operator Eleven",
            12: "Operator Twelve",
        },
        operator_weights_by_block={
            122: {10: 1, 11: 4},
            123: {10: 1, 11: 4, 12: 5},
        },
    )
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=object(),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=meta_registry,
        ),
        notifiable_events={"OperatorGroupUpdated"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="OperatorGroupUpdated",
        args={
            "groupId": 7,
            "groupInfo": {
                "name": "New Group",
                "subNodeOperators": [
                    {"nodeOperatorId": 10, "share": 4},
                    {"nodeOperatorId": 11, "share": 1},
                    {"nodeOperatorId": 12, "share": 2},
                ],
            },
        },
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert isinstance(plan, NotificationPlan)
    operator_messages = _operator_messages(plan)
    assert set(operator_messages) == {"10", "11", "12"}
    assert "Group: `7`" in operator_messages["10"]
    assert "Group renamed: `Old Group` \\-\\> `New Group`" in operator_messages["10"]
    assert "Node Operator:" not in operator_messages["10"]
    assert "Group renamed: `Old Group` \\-\\> `New Group`" in operator_messages["11"]
    assert "Node Operator:" not in operator_messages["11"]
    assert "Added \\#12 \\- Operator Twelve" in operator_messages["12"]
    assert "Operator Twelve" in operator_messages["12"]
    assert "Group: `7`" in operator_messages["12"]
    assert "Group renamed: `Old Group` \\-\\> `New Group`" in operator_messages["12"]


@pytest.mark.asyncio
async def test_curated_operator_group_cleared_lists_all_affected_node_operators():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    meta_registry = _FakeMetaRegistry(
        {
            "name": "Test Group",
            "subNodeOperators": [
                {"nodeOperatorId": 10, "share": 4},
                {"nodeOperatorId": 11, "share": 1},
            ],
        },
        metadata_names={10: "Operator Ten", 11: "Operator Eleven"},
        operator_weights_by_block={122: {10: 1, 11: 4}},
    )
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=object(),
            accounting=object(),
            parameters_registry=object(),
            meta_registry=meta_registry,
        ),
        notifiable_events={"OperatorGroupCleared"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="OperatorGroupCleared",
        args={"groupId": 7},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert isinstance(plan, NotificationPlan)
    assert meta_registry.group_ids == [7]
    assert meta_registry.call.calls == [{"block_identifier": 122}]
    assert _broadcast_operator_ids(plan) == {"10", "11"}
    message = _broadcast(plan)
    assert message is not None
    assert "Operator group cleared" in message
    assert "Group: `7: Test Group`" in message
    assert "Affected Node Operators" in message
    assert "Operator Ten" in message
    assert "Weighted share" not in message
    assert "group share" not in message
    assert (
        "These Node Operators will no longer receive deposit allocation through this group"
        in message
    )
    assert "Effective weight" not in message
    assert "Operator Eleven" in message
    assert "nodeOperatorId" not in message
    assert meta_registry.operator_weight_ids == []
    assert meta_registry.operator_weight_calls == []


@pytest.mark.asyncio
async def test_curated_bond_curve_weight_set_targets_mapped_node_operators():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages
    from sentinel.notifications import NotificationPlan

    _set_event_config()
    module = _FakeCuratedModule(operators_count=3)
    accounting = _FakeCuratedAccounting({0: 1, 1: 2, 2: 1})
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=module,
            accounting=accounting,
            parameters_registry=object(),
            meta_registry=_FakeMetaRegistry(metadata_names={0: "Operator Zero", 2: "Operator Two"}),
        ),
        notifiable_events={"BondCurveWeightSet"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="BondCurveWeightSet",
        args={"curveId": 1, "weight": 50000},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert isinstance(plan, NotificationPlan)
    assert module.operators_count_call.calls == [{"block_identifier": 123}]
    assert accounting.curve_id_calls == [0, 1, 2]
    operator_messages = _operator_messages(plan)
    assert set(operator_messages) == {"0", "2"}
    assert "Operator type weight changed" in operator_messages["0"]
    assert "Type id: `1`" in operator_messages["0"]
    assert "New weight: `50000`" in operator_messages["0"]
    assert "Node Operator: \\#0 \\- Operator Zero" in operator_messages["0"]
    assert "Node Operator: \\#2 \\- Operator Two" in operator_messages["2"]


@pytest.mark.asyncio
async def test_curated_bond_curve_weight_set_skips_unassigned_curve():
    from sentinel.models import Event
    from sentinel.modules.curated.events import CuratedEventMessages

    _set_event_config()
    module = _FakeCuratedModule(operators_count=3)
    accounting = _FakeCuratedAccounting({0: 1, 1: 2, 2: 1})
    adapter = _FakeCuratedAdapter(
        contracts=SimpleNamespace(
            module=module,
            accounting=accounting,
            parameters_registry=object(),
            meta_registry=_FakeMetaRegistry(),
        ),
        notifiable_events={"BondCurveWeightSet"},
    )
    event_messages = CuratedEventMessages(adapter, distribution_log_fetcher=_FakeFetcher(result={}))
    event = Event(
        event="BondCurveWeightSet",
        args={"curveId": 99, "weight": 50000},
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    plan = await event_messages.get_notification_plan(_notification(event))

    assert plan is None
    assert module.operators_count_call.calls == [{"block_identifier": 123}]
    assert accounting.curve_id_calls == [0, 1, 2]


def test_subscription_uses_v3_event_bindings_only():
    from web3 import AsyncWeb3

    from sentinel.app.contracts import CommunityContractAddresses
    from sentinel.app.module_adapter import build_module_adapter_from_config
    from sentinel.module_types import ModuleType
    from sentinel.web3_events import build_event_bindings

    cfg = SimpleNamespace(
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
        module_ui_url=None,
        process_blocks_requests_per_second=None,
    )
    set_config(cfg)
    try:
        w3 = AsyncWeb3()
        module_adapter = build_module_adapter_from_config(cfg, w3, SharedChainConnection(w3))
        event_bindings = build_event_bindings(module_adapter)
        decoded_event_names = {
            event_abi["name"] for event_abi in event_bindings.abi_by_topics.values()
        }
        assert "Initialized" not in decoded_event_names
        assert "ELRewardsStealingPenaltyReported" not in decoded_event_names
        assert "WithdrawalSubmitted" not in decoded_event_names
        assert "GeneralDelayedPenaltyReported" in decoded_event_names
        assert "ValidatorSlashingReported" in decoded_event_names
        assert "ModuleFeeDistributed" in decoded_event_names
        assert "DistributionLogUpdated" in decoded_event_names
        assert "KeyAllocatedBalanceChanged" not in decoded_event_names
    finally:
        clear_config()


def test_topics_to_follow_deduplicates_compatible_event_topics():
    from sentinel.web3_events import topics_to_follow

    first_event = {
        "type": "event",
        "name": "ValidatorExitDelayProcessed",
        "inputs": [
            {"name": "nodeOperatorId", "type": "uint256", "indexed": True},
            {"name": "pubkey", "type": "bytes", "indexed": False},
            {"name": "delayFee", "type": "uint256", "indexed": False},
        ],
        "anonymous": False,
    }
    second_event = {
        "type": "event",
        "name": "ValidatorExitDelayProcessed",
        "inputs": [
            {"name": "nodeOperatorId", "type": "uint256", "indexed": True},
            {"name": "pubkey", "type": "bytes", "indexed": False},
            {"name": "delayFee", "type": "uint256", "indexed": False},
        ],
        "anonymous": False,
    }

    topics = topics_to_follow({"ValidatorExitDelayProcessed"}, [first_event], [second_event])

    assert len(topics) == 1
    [event_abi] = topics.values()
    assert event_abi["inputs"][2]["name"] == "delayFee"


def test_topics_to_follow_rejects_incompatible_same_topic_abis():
    from sentinel.web3_events import topics_to_follow

    indexed_event = {
        "type": "event",
        "name": "SameTopic",
        "inputs": [{"name": "value", "type": "uint256", "indexed": True}],
        "anonymous": False,
    }
    non_indexed_event = {
        "type": "event",
        "name": "SameTopic",
        "inputs": [{"name": "value", "type": "uint256", "indexed": False}],
        "anonymous": False,
    }

    with pytest.raises(RuntimeError, match="incompatible ABI inputs"):
        topics_to_follow({"SameTopic"}, [indexed_event], [non_indexed_event])


@pytest.mark.asyncio
async def test_validator_slashing_reported_handler_formats_pubkey_and_footer():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.models import Event

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.chain = _DummyConnectProvider()
    event_messages.cfg = SimpleNamespace(
        beaconchain_url_template="https://beaconcha.in/validator/{}",
        etherscan_tx_url_template="https://etherscan.io/tx/{}",
    )

    event = Event(
        event="ValidatorSlashingReported",
        args={"nodeOperatorId": 42, "keyIndex": 7, "pubkey": bytes.fromhex("1234")},
        block=1,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    message = await CommunityEventMessages.validator_slashing_reported(
        event_messages, _notification(event)
    )

    assert "Validator slashing reported" in message
    assert "[0x1234](https://beaconcha.in/validator/0x1234)" in message
    assert "Key index: `7`" in message
    assert "nodeOperatorId: 42" in message
    assert "[Transaction](https://etherscan.io/tx/0xdeadbeef)" in message


@pytest.mark.asyncio
async def test_validator_exit_request_uses_curve_allowed_exit_delay():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.models import Event

    accounting = _FakeCuratedAccounting({42: 7})
    parameters_registry = _FakeParametersRegistry({7: 2 * 24 * 60 * 60})
    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.chain = _DummyConnectProvider()
    event_messages.accounting = accounting
    event_messages.parametersRegistry = parameters_registry
    event_messages.cfg = SimpleNamespace(
        beaconchain_url_template="https://beaconcha.in/validator/{}",
        etherscan_tx_url_template="https://etherscan.io/tx/{}",
    )

    event = Event(
        event="ValidatorExitRequest",
        args={
            "nodeOperatorId": 42,
            "validatorPubkey": bytes.fromhex("1234"),
            "timestamp": 1_700_000_000,
        },
        block=123,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    message = await CommunityEventMessages.validator_exit_request(
        event_messages, _notification(event)
    )

    assert accounting.curve_id_calls == [42]
    assert accounting.curve_id_call_objects[0].calls == [{"block_identifier": 123}]
    assert parameters_registry.allowed_exit_delay_calls == [7]
    assert parameters_registry.allowed_exit_delay_call_objects[0].calls == [
        {"block_identifier": 123}
    ]
    assert "Request date: `Tue 14 Nov 2023, 10:13PM UTC`" in message
    assert "Make sure to exit the key before Thu 16 Nov 2023, 10:13PM UTC" in message
    assert "nodeOperatorId: 42" in message


@pytest.mark.asyncio
async def test_validator_exit_requests_are_batched_per_node_operator():
    from sentinel.models import Event, EventNotification
    from sentinel.modules.community.events import CommunityEventMessages

    accounting = _FakeCuratedAccounting({42: 7})
    parameters_registry = _FakeParametersRegistry({7: 2 * 24 * 60 * 60})
    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.chain = _DummyConnectProvider()
    event_messages.accounting = accounting
    event_messages.parametersRegistry = parameters_registry
    event_messages.cfg = SimpleNamespace(
        beaconchain_url_template="https://beaconcha.in/validator/{}",
        etherscan_tx_url_template="https://etherscan.io/tx/{}",
    )

    key_1 = "12" * 32
    key_2 = "34" * 32
    short_key_1 = f"0x{key_1[:8]}\\.\\.\\.{key_1[-8:]}"
    short_key_2 = f"0x{key_2[:8]}\\.\\.\\.{key_2[-8:]}"
    key_1_with_prefix = f"0x{key_1}"
    key_2_with_prefix = f"0x{key_2}"
    validator_pubkeys = [key_1, key_2]
    events = tuple(
        Event(
            event="ValidatorExitRequest",
            args={
                "nodeOperatorId": 42,
                "validatorPubkey": bytes.fromhex(pubkey),
                "timestamp": 1_700_000_000,
            },
            block=123,
            tx=HexBytes("0xdeadbeef"),
            address="0x0000000000000000000000000000000000000000",
            log_index=log_index,
            transaction_index=0,
        )
        for log_index, pubkey in enumerate(validator_pubkeys)
    )

    message = await CommunityEventMessages.validator_exit_request(
        event_messages, EventNotification(events)
    )

    assert accounting.curve_id_calls == [42]
    assert parameters_registry.allowed_exit_delay_calls == [7]
    assert "Validator exits requested" in message
    assert "Make sure to exit these keys before Thu 16 Nov 2023, 10:13PM UTC" in message
    assert "Requested keys:" in message
    assert (
        f"Validator 1: [{short_key_1}](https://beaconcha.in/validator/{key_1_with_prefix})"
        in message
    )
    assert (
        f"Validator 2: [{short_key_2}](https://beaconcha.in/validator/{key_2_with_prefix})"
        in message
    )
    assert "Request date: `Tue 14 Nov 2023, 10:13PM UTC`" in message
    assert "nodeOperatorId: 42" in message


@pytest.mark.asyncio
async def test_validator_withdrawals_are_batched_across_five_blocks():
    from sentinel.models import Event, EventNotification
    from sentinel.modules.aggregation import AggregationGroups
    from sentinel.modules.community.events import (
        COMMUNITY_EVENTS_TO_FOLLOW,
        CommunityEventMessages,
    )

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.chain = _DummyConnectProvider()
    event_messages.cfg = SimpleNamespace(
        beaconchain_url_template="https://beaconcha.in/validator/{}",
        etherscan_block_url_template="https://etherscan.io/block/{}",
    )

    events = tuple(
        Event(
            event="ValidatorWithdrawn",
            args={
                "nodeOperatorId": 42,
                "pubkey": bytes.fromhex(pubkey),
                "exitBalance": exit_balance,
                "slashingPenalty": slashing_penalty,
            },
            block=block,
            tx=HexBytes(tx),
            address="0x0000000000000000000000000000000000000000",
            log_index=0,
            transaction_index=0,
        )
        for pubkey, exit_balance, slashing_penalty, block, tx in (
            ("12" * 48, 32 * 10**18, 0, 100, "0x01"),
            ("34" * 48, 31 * 10**18, 10**18, 104, "0x02"),
        )
    )

    message = await CommunityEventMessages.validator_withdrawn(
        event_messages, EventNotification(events)
    )

    aggregation_group = COMMUNITY_EVENTS_TO_FOLLOW["ValidatorWithdrawn"].aggregation_group
    assert aggregation_group == AggregationGroups.VALIDATOR_WITHDRAWALS
    assert aggregation_group.window_blocks == 5
    assert "Validator withdrawals confirmed" in message
    assert "Validator 1" in message
    assert "Validator 2" in message
    assert "Exit balance: `32 ether`" in message
    assert "Exit balance: `31 ether`" in message
    assert "Slashing penalty: `1 ether`" in message
    assert r"Blocks: [100](https://etherscan.io/block/100) \.\.\. " in message
    assert "[104](https://etherscan.io/block/104)" in message
    assert "nodeOperatorId: 42" in message


@pytest.mark.asyncio
async def test_validator_exit_delay_processed_accepts_v3_delay_fee_arg():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.models import Event

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.chain = _DummyConnectProvider()
    event_messages.cfg = SimpleNamespace(
        beaconchain_url_template="https://beaconcha.in/validator/{}",
        etherscan_tx_url_template="https://etherscan.io/tx/{}",
    )

    event = Event(
        event="ValidatorExitDelayProcessed",
        args={"nodeOperatorId": 42, "pubkey": bytes.fromhex("1234"), "delayFee": 10**18},
        block=1,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    message = await CommunityEventMessages.validator_exit_delay_processed(
        event_messages, _notification(event)
    )

    assert "Validator exit delay processed" in message
    assert "[0x1234](https://beaconcha.in/validator/0x1234)" in message
    assert "Delay penalty: `1 ether`" in message


@pytest.mark.asyncio
async def test_key_allocated_balance_changed_handler_humanizes_balance():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.models import Event

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.cfg = SimpleNamespace(
        etherscan_tx_url_template="https://etherscan.io/tx/{}",
    )

    event = Event(
        event="KeyAllocatedBalanceChanged",
        args={"nodeOperatorId": 42, "keyIndex": 7, "newTotal": 10**18},
        block=1,
        tx=HexBytes("0xdeadbeef"),
        address="0x0000000000000000000000000000000000000000",
        log_index=0,
        transaction_index=0,
    )

    message = await CommunityEventMessages.key_allocated_balance_changed(
        event_messages, _notification(event)
    )

    assert "Key balance increased" in message
    assert "Key index: `7`" in message
    assert "New allocated balance: `1 ether`" in message
    assert "nodeOperatorId: 42" in message


@pytest.mark.asyncio
async def test_deposited_signing_keys_count_changed_handler_renders_aggregated_event():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.models import Event, EventNotification

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.cfg = SimpleNamespace(
        etherscan_tx_url_template="https://etherscan.io/tx/{}",
        etherscan_block_url_template="https://etherscan.io/block/{}",
    )
    event_messages.module = _FakeCuratedModule(
        0, {42: SimpleNamespace(totalAddedKeys=0, totalDepositedKeys=1)}
    )

    source_events = (
        Event(
            event="DepositedSigningKeysCountChanged",
            args={"nodeOperatorId": 42, "depositedKeysCount": 2},
            block=123,
            tx=HexBytes("0xdeadbeef"),
            address="0x0000000000000000000000000000000000000000",
            log_index=0,
            transaction_index=0,
        ),
        Event(
            event="DepositedSigningKeysCountChanged",
            args={"nodeOperatorId": 42, "depositedKeysCount": 3},
            block=123,
            tx=HexBytes("0xfeedbeef"),
            address="0x0000000000000000000000000000000000000000",
            log_index=0,
            transaction_index=0,
        ),
    )
    event = EventNotification(source_events=source_events)

    plan = await CommunityEventMessages.deposited_signing_keys_count_changed(event_messages, event)
    (message,) = _per_chat(plan).render(frozenset({"42"}))

    assert message == (
        "🤩 *Deposit digest*\n\n"
        "Node Operators:\n"
        "\\- \\#42: `1 \\-\\> 3`\n\n"
        "Block: [123](https://etherscan.io/block/123)"
    )
    assert event_messages.module.node_operator_calls == [42]
    assert event_messages.module.node_operator_call_objects[0].calls == [{"block_identifier": 122}]


@pytest.mark.asyncio
async def test_total_signing_keys_count_changed_handler_renders_aggregated_event():
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.models import Event, EventNotification

    event_messages = CommunityEventMessages.__new__(CommunityEventMessages)
    event_messages.cfg = SimpleNamespace(
        etherscan_tx_url_template="https://etherscan.io/tx/{}",
        etherscan_block_url_template="https://etherscan.io/block/{}",
    )
    event_messages.module = SimpleNamespace(
        functions=SimpleNamespace(
            getNodeOperator=lambda _node_operator_id: _FakeCall(SimpleNamespace(totalAddedKeys=2))
        )
    )

    source_events = (
        Event(
            event="TotalSigningKeysCountChanged",
            args={"nodeOperatorId": 42, "totalKeysCount": 3},
            block=123,
            tx=HexBytes("0xdeadbeef"),
            address="0x0000000000000000000000000000000000000000",
            log_index=0,
            transaction_index=0,
        ),
        Event(
            event="TotalSigningKeysCountChanged",
            args={"nodeOperatorId": 42, "totalKeysCount": 5},
            block=125,
            tx=HexBytes("0xfeedbeef"),
            address="0x0000000000000000000000000000000000000000",
            log_index=0,
            transaction_index=0,
        ),
    )
    event = EventNotification(source_events=source_events)

    message = await CommunityEventMessages.total_signing_keys_count_changed(event_messages, event)

    assert "New keys uploaded" in message
    assert "Keys count: `2 \\-\\> 5`" in message
    assert "nodeOperatorId: 42" in message
    assert (
        "Blocks: [123](https://etherscan.io/block/123) \\.\\.\\. [125](https://etherscan.io/block/125)"
        in message
    )
