import os
import pytest

from sentinel.config import clear_config

from .helpers import build_subscription


CURATED_HOODI_MODULE = "0x87EB69Ae51317405FD285efD2326a4a11f6173b9"


@pytest.fixture(autouse=True, scope="session")
def curated_hoodi_config_env():
    """Point this suite at the Hoodi Curated deployment used by the fixture blocks."""

    provider_url = os.getenv("WEB3_SOCKET_PROVIDERS") or os.getenv("WEB3_SOCKET_PROVIDER")
    if not provider_url:
        pytest.skip("WEB3_SOCKET_PROVIDERS or WEB3_SOCKET_PROVIDER is required")

    with pytest.MonkeyPatch.context() as m:
        m.setenv("WEB3_SOCKET_PROVIDERS", provider_url)
        m.setenv("MODULE_ADDRESS", CURATED_HOODI_MODULE)
        m.setenv("ETHERSCAN_URL", "https://etherscan.io")
        m.setenv("BEACONCHAIN_URL", "https://beaconcha.in")
        m.setenv("MODULE_UI_URL", "https://lido.fi")
        clear_config()
        yield
    clear_config()


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _has_expected_message(harness, *, event_name: str, expected_markdown: str | None) -> bool:
    messages = []
    for event, plan in harness.processed_events:
        if event is not None and event.event == event_name:
            messages.append(plan.broadcast if plan else None)
    if not messages:
        return False
    if expected_markdown is None:
        return all(message is None for message in messages)
    return expected_markdown in messages


def _has_expected_node_operator_messages(
    harness, *, event_name: str, expected_messages: dict[str, str]
) -> bool:
    messages_by_operator: dict[str, set[str]] = {}
    for event, plan in harness.processed_events:
        if event is None or event.event != event_name or plan is None:
            continue
        for node_operator_id, message in plan.per_node_operator.items():
            messages_by_operator.setdefault(node_operator_id, set()).add(message)

    return all(
        any(
            expected_message in actual_message
            for actual_message in messages_by_operator.get(node_operator_id, set())
        )
        for node_operator_id, expected_message in expected_messages.items()
    )


async def _exercise_curated_event(
    *,
    event_name: str,
    fork_block: int,
    expected_markdown: str | None,
    anvil_launcher,
    expected_per_node: dict[str, str] | None = None,
) -> None:
    anvil = await anvil_launcher(fork_block)
    harness = await build_subscription(anvil.ws_url, anvil.http_url)
    try:
        await harness.replay_blocks(fork_block - 1, fork_block)
        assert _has_expected_message(
            harness, event_name=event_name, expected_markdown=expected_markdown
        ), (
            f"Did not find expected Curated message for event {event_name}, \n"
            f"{expected_markdown=}\n"
            f"found={[plan.broadcast if plan else None for event, plan in harness.processed_events]}"
        )
        if expected_per_node is not None:
            assert _has_expected_node_operator_messages(
                harness, event_name=event_name, expected_messages=expected_per_node
            ), (
                f"Did not find expected Curated per-node message for event {event_name}, \n"
                f"{expected_per_node=}\n"
                f"found={[(event.event if event else None, plan.per_node_operator if plan else None) for event, plan in harness.processed_events]}"
            )
    finally:
        await harness.disconnect()


async def test_curated_process_blocks_resumed_release(anvil_launcher):
    await _exercise_curated_event(
        event_name="Resumed",
        fork_block=2722011,
        expected_markdown=(
            "🎉 *Curated Module is live\\!*\n\n"
            "Check the [Curated Module UI](https://lido.fi) "
            "for operator workflows and current module details\\.\n\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_bond_deposited_eth(anvil_launcher):
    await _exercise_curated_event(
        event_name="BondDepositedETH",
        fork_block=2760979,
        expected_markdown=(
            "✅ *Bond deposited*\n\n"
            "From: `0x9BC9ffe091DEa5dBD9E5b85e43F36D43D600eCE4`\n"
            "Amount: `12\\.5 ETH`\n\n"
            "Node Operator: \\#0 \\- Attestant \\(BVI\\) Limited\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_deposited_signing_keys_count_changed(
    anvil_launcher,
):
    await _exercise_curated_event(
        event_name="DepositedSigningKeysCountChanged",
        fork_block=2766515,
        expected_markdown=(
            "🤩 *Keys were deposited\\!*\n\n"
            "Deposited keys count: `0 \\-\\> 16`\n\n"
            "Node Operator: \\#2 \\- Develp PTO\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_bond_claimed_steth(anvil_launcher):
    await _exercise_curated_event(
        event_name="BondClaimedStETH",
        fork_block=2798407,
        expected_markdown=(
            "✅ *Bond claimed*\n\n"
            "Recipient: `0x330881A81Abf9437Ed877B3a8BB119A952af7d66`\n"
            "Amount: `0\\.003284375825565586 stETH`\n\n"
            "Node Operator: \\#3 \\- Consensys Curated Operator 1\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_node_operator_effective_weight_changed(
    anvil_launcher,
):
    await _exercise_curated_event(
        event_name="NodeOperatorEffectiveWeightChanged",
        fork_block=2766235,
        expected_markdown=(
            "ℹ️ *Operator effective weight changed*\n\n"
            "Effective weight: `0 \\-\\> 100000`\n\n"
            "Node Operator: \\#0 \\- Attestant \\(BVI\\) Limited\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_distribution_log_updated(anvil_launcher):
    await _exercise_curated_event(
        event_name="DistributionLogUpdated",
        fork_block=2773878,
        expected_markdown=(
            "📈 *Rewards distributed\\!*\n\n"
            "Follow the [Curated Module UI](https://lido.fi) "
            "to check new claimable rewards\\.\n\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_bond_curve_weight_set(anvil_launcher):
    await _exercise_curated_event(
        event_name="BondCurveWeightSet",
        fork_block=2662659,
        expected_markdown=None,
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_operator_group_created(anvil_launcher):
    await _exercise_curated_event(
        event_name="OperatorGroupCreated",
        fork_block=2892439,
        expected_markdown=(
            "ℹ️ *Operator group created*\n\n"
            "Group: `19: Sigma Prime`\n"
            "Added Node Operators:\n"
            "\\- \\#31 \\- Sigma Prime\n"
            "  Weighted share: 100% \\(group share: 100%\\)\n"
            "\\- \\#45 \\- Sigma Prime\n"
            "  Weighted share: 0% \\(group share: 0%\\)\n\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_operator_group_updated(anvil_launcher):
    await _exercise_curated_event(
        event_name="OperatorGroupUpdated",
        fork_block=2892438,
        expected_markdown=None,
        expected_per_node={
            "0": ("Share: `50% \\-\\> 100%`\n  Effective allocation share: `50% \\-\\> 100%`"),
            "1": ("Share: `50% \\-\\> 0%`\n  Effective allocation share: `50% \\-\\> 0%`"),
        },
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_operator_metadata_set(anvil_launcher):
    await _exercise_curated_event(
        event_name="OperatorMetadataSet",
        fork_block=2753441,
        expected_markdown=(
            "ℹ️ *Operator metadata changed*\n\n"
            "Name: `Attestant \\(BVI\\) Limited`\n"
            "Description: `Attestant \\(BVI\\) Limited are a curated node operator and the team behind both Dirk "
            "\\(distributed key manager\\) and Vouch \\(multi\\-node Ethereum consensus client\\)\\. We focus on "
            "secure, professional validator operations and have built a lot of the open\\-source tooling that "
            "underpins best\\-practice staking infrastructure\\.`\n\n"
            "Node Operator: \\#0 \\- Attestant \\(BVI\\) Limited\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_total_signing_keys_count_changed(
    anvil_launcher,
):
    await _exercise_curated_event(
        event_name="TotalSigningKeysCountChanged",
        fork_block=2760979,
        expected_markdown=(
            "👀 *New keys uploaded*\n\n"
            "Keys count: `0 \\-\\> 16`\n\n"
            "Node Operator: \\#0 \\- Attestant \\(BVI\\) Limited\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_node_operator_manager_address_change_proposed(
    anvil_launcher,
):
    await _exercise_curated_event(
        event_name="NodeOperatorManagerAddressChangeProposed",
        fork_block=2772737,
        expected_markdown=(
            "ℹ️ *New manager address proposed*\n\n"
            "Proposed address: `0xA9113E3632FB4Fa1B5eC6b00a2bBD345BEF8b293`\n\n"
            "To complete the change, the Node Operator must confirm it from the new address\\.\n\n"
            "Node Operator: \\#10 \\- QA Operator\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_node_operator_manager_address_changed(
    anvil_launcher,
):
    await _exercise_curated_event(
        event_name="NodeOperatorManagerAddressChanged",
        fork_block=2772761,
        expected_markdown=(
            "✅ *Manager address changed*\n\n"
            "New address: `0xA9113E3632FB4Fa1B5eC6b00a2bBD345BEF8b293`\n\n"
            "Node Operator: \\#10 \\- QA Operator\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_node_operator_reward_address_changed(
    anvil_launcher,
):
    await _exercise_curated_event(
        event_name="NodeOperatorRewardAddressChanged",
        fork_block=2805023,
        expected_markdown=(
            "✅ *Rewards address changed*\n\n"
            "New address: `0x5fDCb78cA9A1164c13428E5fC9582c8c48Dab69f`\n\n"
            "Node Operator: \\#28 \\- Stakely DVT\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_node_operator_reward_address_change_proposed(
    anvil_launcher,
):
    await _exercise_curated_event(
        event_name="NodeOperatorRewardAddressChangeProposed",
        fork_block=2805052,
        expected_markdown=(
            "ℹ️ *New rewards address proposed*\n\n"
            "Proposed address: `0x75Ce9d9C53f08B96D88f8FD6494d0B664be16878`\n\n"
            "To complete the change, the Node Operator must confirm it from the new address\\.\n\n"
            "Node Operator: \\#28 \\- Stakely DVT\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_bond_curve_set(anvil_launcher):
    await _exercise_curated_event(
        event_name="BondCurveSet",
        fork_block=2753441,
        expected_markdown=(
            "ℹ️ *Operator type changed*\n\n"
            "New type id: `1`\n"
            "Operational requirements may now differ\\. "
            "Check the [Curated Module UI](https://lido.fi) for updated guidance\\.\n\n"
            "Node Operator: \\#0 \\- Attestant \\(BVI\\) Limited\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_bond_deposited_steth(anvil_launcher):
    await _exercise_curated_event(
        event_name="BondDepositedStETH",
        fork_block=2766428,
        expected_markdown=(
            "✅ *Bond deposited*\n\n"
            "From: `0x6117ED3095b58298B3223dBD474281ED4112e845`\n"
            "Amount: `0\\.8 stETH`\n\n"
            "Node Operator: \\#2 \\- Develp PTO\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_bond_lock_period_changed(anvil_launcher):
    await _exercise_curated_event(
        event_name="BondLockPeriodChanged",
        fork_block=2662648,
        expected_markdown=(
            "ℹ️ *Bond lock period changed*\n\n"
            "New period: `60 days, 0:00:00`\n\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_target_validators_count_changed(anvil_launcher):
    await _exercise_curated_event(
        event_name="TargetValidatorsCountChanged",
        fork_block=3219085,
        expected_markdown=(
            "🚨 *Target validators count changed*\n\n"
            "The limit has been set to 180\\.\n"
            "4 keys above the limit will be requested to exit immediately\\.\n\n"
            "Node Operator: \\#0 \\- Attestant \\(BVI\\) Limited\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_process_blocks_forced_exit_target_limit_unset(anvil_launcher):
    await _exercise_curated_event(
        event_name="TargetValidatorsCountChanged",
        fork_block=3252073,
        expected_markdown=(
            "✅ *Unsetting forced exit target limit*\n\n"
            "The forced target validator limit of `180` has been removed\\.\n"
            "No additional validators will be requested to exit\\.\n\n"
            "Node Operator: \\#0 \\- Attestant \\(BVI\\) Limited\n"
            "[Transaction](https://etherscan.io/tx/0xdeadbeef)"
        ),
        anvil_launcher=anvil_launcher,
    )


async def test_curated_batches_validator_withdrawals_across_five_blocks(anvil_launcher):
    first_withdrawal_block = 3232454
    window_end_block = first_withdrawal_block + 4
    anvil = await anvil_launcher(window_end_block)
    harness = await build_subscription(anvil.ws_url, anvil.http_url)
    try:
        await harness.replay_blocks(first_withdrawal_block - 1, window_end_block)

        processed = [
            (event, plan)
            for event, plan in harness.processed_events
            if event.event == "ValidatorWithdrawn"
        ]
        assert [event.block for event, _ in processed] == [
            3232454,
            3232455,
            3232456,
            3232457,
        ]

        messages = {plan.broadcast for _, plan in processed if plan is not None}
        assert len(messages) == 1
        message = messages.pop()
        assert "Validator withdrawals confirmed" in message
        assert message.count("\\- Validator ") == 4
        assert r"Blocks: [3232454](https://etherscan.io/block/3232454) \.\.\. " in message
        assert "[3232457](https://etherscan.io/block/3232457)" in message
        assert "Node Operator: \\#0 \\- Attestant \\(BVI\\) Limited" in message
    finally:
        await harness.disconnect()
