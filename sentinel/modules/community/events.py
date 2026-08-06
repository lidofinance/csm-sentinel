from typing import TYPE_CHECKING

from eth_utils import humanize_wei

from sentinel.models import Event, EventHandler, EventNotification
from sentinel.modules.base_events import BaseModule
from sentinel.modules.aggregation import AggregationGroups
from sentinel.modules.community.adapter import COMMUNITY_EVENTS
from sentinel.modules.community.texts import (
    COMMUNITY_EVENT_DESCRIPTIONS,
    COMMUNITY_EVENT_MESSAGES,
    event_block_footer,
    event_block_footer_tx_only,
    event_transaction_footer,
    event_transaction_footer_tx_only,
)
from sentinel.modules.distribution import DistributionLogFetcher, default_distribution_log_fetcher
from sentinel.modules.registry import RegisterEventHandler

if TYPE_CHECKING:
    from sentinel.modules.base import ModuleAdapter

COMMUNITY_EVENTS_TO_FOLLOW: dict[str, EventHandler] = {}


def register_event(event_name: str, aggregation_group=None):
    return RegisterEventHandler(
        COMMUNITY_EVENTS_TO_FOLLOW,
        event_name,
        aggregation_group=aggregation_group,
    )


def assert_event_mappings() -> None:
    catalog_events = set(COMMUNITY_EVENTS)
    events = set(COMMUNITY_EVENTS_TO_FOLLOW.keys())
    messages = set(COMMUNITY_EVENT_MESSAGES.keys())
    descriptions = set(COMMUNITY_EVENT_DESCRIPTIONS.keys())
    assert catalog_events == events, "Missed events: " + str(
        catalog_events.symmetric_difference(events)
    )
    assert events == messages, "Missed events: " + str(events.symmetric_difference(messages))
    assert events == descriptions, "Missed events: " + str(
        events.symmetric_difference(descriptions)
    )


class CommunityEventMessages(BaseModule):
    event_handlers = COMMUNITY_EVENTS_TO_FOLLOW
    event_messages = COMMUNITY_EVENT_MESSAGES

    def __init__(
        self,
        module_adapter: "ModuleAdapter",
        distribution_log_fetcher: "DistributionLogFetcher | None" = None,
    ):
        self._distribution_log_fetcher = (
            distribution_log_fetcher or default_distribution_log_fetcher
        )
        super().__init__(module_adapter)

    async def event_footer(self, event: Event) -> str:
        tx_link = self.transaction_link(event)
        node_operator_id = event.args.get("nodeOperatorId")
        if node_operator_id is None:
            return event_transaction_footer_tx_only(tx_link).as_markdown()
        return event_transaction_footer(node_operator_id, tx_link).as_markdown()

    async def block_footer(self, event: EventNotification) -> str:
        start_block, end_block = self.notification_block_range(event)
        block_links = [(str(start_block), self.block_link(start_block))]
        if end_block != start_block:
            block_links.append((str(end_block), self.block_link(end_block)))
        node_operator_id = event.args.get("nodeOperatorId")
        if node_operator_id is None:
            return event_block_footer_tx_only(block_links).as_markdown()
        return event_block_footer(node_operator_id, block_links).as_markdown()

    @register_event("ValidatorExitDelayProcessed")
    async def validator_exit_delay_processed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        key, key_url = self.validator_link(event.args["pubkey"])
        penalty = humanize_wei(event.args["delayFee"])
        return template(key, key_url, penalty) + await self.notification_footer(event)

    @register_event("TargetValidatorsCountChanged")
    async def target_validators_count_changed(self, event: EventNotification):
        node_operator_before = await self.module.functions.getNodeOperator(
            event.args["nodeOperatorId"]
        ).call(block_identifier=event.block - 1)
        node_operator = await self.module.functions.getNodeOperator(
            event.args["nodeOperatorId"]
        ).call(block_identifier=event.block)
        active_validators_count = node_operator.totalDepositedKeys - node_operator.totalExitedKeys

        template = self._require_message_template(event.event)
        return template(
            node_operator_before.targetLimitMode,
            node_operator_before.targetLimit,
            event.args["targetLimitMode"],
            event.args["targetValidatorsCount"],
            active_validators_count,
        ) + await self.notification_footer(event)


register_event(
    "DepositedSigningKeysCountChanged",
    aggregation_group=AggregationGroups.DEPOSITED_SIGNING_KEY_COUNTS,
)(CommunityEventMessages.deposited_signing_keys_count_changed)
register_event(
    "TotalSigningKeysCountChanged",
    aggregation_group=AggregationGroups.TOTAL_SIGNING_KEY_COUNTS,
)(CommunityEventMessages.total_signing_keys_count_changed)
register_event("VettedSigningKeysCountDecreased")(
    CommunityEventMessages.vetted_signing_keys_count_decreased
)
register_event("KeyRemovalChargeApplied")(CommunityEventMessages.key_removal_charge_applied)
register_event("KeyAllocatedBalanceChanged")(CommunityEventMessages.key_allocated_balance_changed)
register_event("BondCurveSet")(CommunityEventMessages.bond_curve_set)
register_event("NodeOperatorManagerAddressChangeProposed")(
    CommunityEventMessages.node_operator_manager_address_change_proposed
)
register_event("NodeOperatorManagerAddressChanged")(
    CommunityEventMessages.node_operator_manager_address_changed
)
register_event("NodeOperatorRewardAddressChangeProposed")(
    CommunityEventMessages.node_operator_reward_address_change_proposed
)
register_event("NodeOperatorRewardAddressChanged")(
    CommunityEventMessages.node_operator_reward_address_changed
)
register_event("CustomRewardsClaimerSet")(CommunityEventMessages.custom_rewards_claimer_set)
register_event("FeeSplitsSet")(CommunityEventMessages.fee_splits_set)
register_event("BondDebtIncreased")(CommunityEventMessages.bond_debt_increased)
register_event("BondDebtCovered")(CommunityEventMessages.bond_debt_covered)
register_event("BondLockRemoved")(CommunityEventMessages.bond_lock_removed)
register_event("GeneralDelayedPenaltyReported")(
    CommunityEventMessages.general_delayed_penalty_reported
)
register_event("GeneralDelayedPenaltySettled")(
    CommunityEventMessages.general_delayed_penalty_settled
)
register_event("GeneralDelayedPenaltyCancelled")(
    CommunityEventMessages.general_delayed_penalty_cancelled
)
register_event("GeneralDelayedPenaltyCompensated")(
    CommunityEventMessages.general_delayed_penalty_compensated
)
register_event("ValidatorSlashingReported")(CommunityEventMessages.validator_slashing_reported)
register_event(
    "ValidatorExitRequest",
    aggregation_group=AggregationGroups.VALIDATOR_EXIT_REQUESTS,
)(CommunityEventMessages.validator_exit_request)
register_event("TriggeredExitFeeRecorded")(CommunityEventMessages.triggered_exit_fee_recorded)
register_event("StrikesPenaltyProcessed")(CommunityEventMessages.strikes_penalty_processed)
register_event(
    "ValidatorWithdrawn",
    aggregation_group=AggregationGroups.VALIDATOR_WITHDRAWALS,
)(CommunityEventMessages.validator_withdrawn)
register_event("DistributionLogUpdated")(CommunityEventMessages.distribution_log_updated)
