import datetime
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from async_lru import alru_cache
from eth_utils import humanize_wei

from sentinel.models import Event, EventHandler, EventNotification
from sentinel.modules.aggregation import AggregationGroups
from sentinel.modules.base_events import BaseModule, _format_date
from sentinel.modules.curated.adapter import CURATED_EVENTS
from sentinel.modules.curated.texts import (
    CURATED_EVENT_DESCRIPTIONS,
    CURATED_EVENT_MESSAGES,
    event_block_footer,
    event_block_footer_tx_only,
    event_block_footer_with_operator_name,
    event_transaction_footer,
    event_transaction_footer_tx_only,
    event_transaction_footer_with_operator_name,
)
from sentinel.modules.distribution import (
    DistributionLogFetcher,
    default_distribution_log_fetcher,
    parse_distribution_log,
    validator_sort_key,
)
from sentinel.services.digest import DigestGroups
from sentinel.modules.formatting import read_field
from sentinel.modules.registry import RegisterEventHandler
from sentinel.notifications import NotificationPlan

if TYPE_CHECKING:
    from sentinel.modules.curated.adapter import CuratedModuleAdapter

CURATED_EVENTS_TO_FOLLOW: dict[str, EventHandler] = {}
logger = logging.getLogger(__name__)


def register_event(event_name: str, aggregation_group=None, digest_name: str | None = None):
    return RegisterEventHandler(
        CURATED_EVENTS_TO_FOLLOW,
        event_name,
        aggregation_group=aggregation_group,
        digest_name=digest_name,
    )


def assert_event_mappings() -> None:
    catalog_events = set(CURATED_EVENTS)
    events = set(CURATED_EVENTS_TO_FOLLOW.keys())
    messages = set(CURATED_EVENT_MESSAGES.keys())
    descriptions = set(CURATED_EVENT_DESCRIPTIONS.keys())
    assert catalog_events == events, "Missed events: " + str(
        catalog_events.symmetric_difference(events)
    )
    assert events == messages, "Missed events: " + str(events.symmetric_difference(messages))
    assert events == descriptions, "Missed events: " + str(
        events.symmetric_difference(descriptions)
    )


def _operator_group_name(group_info) -> str | None:
    name = read_field(group_info, "name", 0)
    return name if isinstance(name, str) and name else None


def _sub_node_operators(group_info):
    return read_field(group_info, "subNodeOperators", 1)


def _sub_node_operator_ids(group_info) -> set[int]:
    return {
        int(read_field(operator, "nodeOperatorId", 0))
        for operator in _sub_node_operators(group_info)
    }


def _sub_node_operator_shares(group_info) -> dict[int, int]:
    return {
        int(read_field(operator, "nodeOperatorId", 0)): int(read_field(operator, "share", 1))
        for operator in _sub_node_operators(group_info)
    }


def _weight_share_basis_points(share: int, weight: int, total_weighted_share: int) -> int:
    if total_weighted_share <= 0:
        return 0
    return share * weight * 10_000 // total_weighted_share


@dataclass(frozen=True, slots=True)
class NodeOperatorMetadata:
    name: str | None


class CuratedEventMessages(BaseModule):
    event_handlers = CURATED_EVENTS_TO_FOLLOW
    event_messages = CURATED_EVENT_MESSAGES

    def __init__(
        self,
        module_adapter: "CuratedModuleAdapter",
        distribution_log_fetcher: "DistributionLogFetcher | None" = None,
    ):
        self._distribution_log_fetcher = (
            distribution_log_fetcher or default_distribution_log_fetcher
        )
        super().__init__(module_adapter)

    def _bind_module_adapter(self, module_adapter: "CuratedModuleAdapter") -> None:
        super()._bind_module_adapter(module_adapter)
        self.meta_registry = module_adapter.contracts.meta_registry

    @alru_cache(maxsize=512)
    async def _fetch_node_operator_metadata(
        self, node_operator_id: int, block: int
    ) -> NodeOperatorMetadata:
        metadata = await self.meta_registry.functions.getOperatorMetadata(node_operator_id).call(
            block_identifier=block
        )
        return NodeOperatorMetadata(name=read_field(metadata, "name", 0) or None)

    async def _node_operator_metadata_or_none(
        self, node_operator_id: int, block: int
    ) -> NodeOperatorMetadata | None:
        try:
            return await self._fetch_node_operator_metadata(node_operator_id, block)
        except Exception:
            logger.warning(
                "Failed to fetch Curated node operator metadata",
                extra={"node_operator_id": node_operator_id, "block": block},
                exc_info=True,
            )
            return None

    async def event_footer(self, event: Event) -> str:
        tx_link = self.transaction_link(event)
        node_operator_id = event.args.get("nodeOperatorId")
        if node_operator_id is None:
            return event_transaction_footer_tx_only(tx_link).as_markdown()

        node_operator_id = int(node_operator_id)
        metadata = await self._node_operator_metadata_or_none(node_operator_id, event.block)
        if metadata is None or not metadata.name:
            return event_transaction_footer(node_operator_id, tx_link).as_markdown()
        return event_transaction_footer_with_operator_name(
            node_operator_id, metadata.name, tx_link
        ).as_markdown()

    async def block_footer(self, event: EventNotification) -> str:
        start_block, end_block = self.notification_block_range(event)
        block_links = [(str(start_block), self.block_link(start_block))]
        if end_block != start_block:
            block_links.append((str(end_block), self.block_link(end_block)))
        node_operator_id = event.args.get("nodeOperatorId")
        if node_operator_id is None:
            return event_block_footer_tx_only(block_links).as_markdown()

        node_operator_id = int(node_operator_id)
        metadata = await self._node_operator_metadata_or_none(node_operator_id, event.block)
        if metadata is None or not metadata.name:
            return event_block_footer(node_operator_id, block_links).as_markdown()
        return event_block_footer_with_operator_name(
            node_operator_id, metadata.name, block_links
        ).as_markdown()

    async def _node_operator_label(self, node_operator_id: int, block: int) -> str:
        metadata = await self._node_operator_metadata_or_none(node_operator_id, block)
        if metadata is None:
            return f"#{node_operator_id}"

        if not metadata.name:
            return f"#{node_operator_id}"
        return f"#{node_operator_id} - {metadata.name}"

    async def _digest_operator_label(self, node_operator_id: int, block: int) -> str:
        return await self._node_operator_label(node_operator_id, block)

    async def _sub_node_operator_allocations(self, group_info, block: int) -> list[dict]:
        sub_node_operators = _sub_node_operators(group_info)
        node_operator_ids = [
            int(read_field(operator, "nodeOperatorId", 0)) for operator in sub_node_operators
        ]
        weights = await self._fetch_node_operator_weights(tuple(node_operator_ids), block)
        total_weighted_share = sum(
            int(read_field(operator, "share", 1)) * weights[node_operator_id]
            for operator, node_operator_id in zip(
                sub_node_operators, node_operator_ids, strict=True
            )
        )

        return [
            {
                "nodeOperatorId": node_operator_id,
                "share": int(read_field(operator, "share", 1)),
                "effectiveWeight": weights[node_operator_id],
                "weightedShare": _weight_share_basis_points(
                    int(read_field(operator, "share", 1)),
                    weights[node_operator_id],
                    total_weighted_share,
                ),
                "label": await self._node_operator_label(node_operator_id, block),
            }
            for operator, node_operator_id in zip(
                sub_node_operators, node_operator_ids, strict=True
            )
        ]

    async def _sub_node_operator_labels(self, group_info, block: int) -> list[str]:
        return [
            await self._node_operator_label(int(read_field(operator, "nodeOperatorId", 0)), block)
            for operator in _sub_node_operators(group_info)
        ]

    async def _fetch_node_operator_weights(
        self, node_operator_ids: tuple[int, ...], block: int
    ) -> dict[int, int]:
        if not node_operator_ids:
            return {}
        weights = await self.meta_registry.functions.getOperatorWeights(
            list(node_operator_ids)
        ).call(block_identifier=block)
        return {
            node_operator_id: int(weight)
            for node_operator_id, weight in zip(node_operator_ids, weights, strict=True)
        }

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

    @register_event("ValidatorExitDelayProcessed")
    async def validator_exit_delay_processed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        key, key_url = self.validator_link(event.args["pubkey"])
        return template(
            key, key_url, humanize_wei(event.args["delayFee"])
        ) + await self.notification_footer(event)

    # TODO: Remove the temporary release notification after the CMv2 launch.
    @register_event("Resumed")
    async def resumed(self, event: EventNotification):
        if event.address.lower() != self.module_address.lower():
            return None
        await self.module_adapter.refresh_staking_module_id()
        template = self._require_message_template(event.event)
        return template() + await self.notification_footer(event)

    @register_event("BondDepositedETH")
    async def bond_deposited_eth(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            event.args["from"], humanize_wei(event.args["amount"])
        ) + await self.notification_footer(event)

    @register_event("BondDepositedStETH")
    async def bond_deposited_steth(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            event.args["from"], humanize_wei(event.args["amount"])
        ) + await self.notification_footer(event)

    @register_event("BondDepositedWstETH")
    async def bond_deposited_wsteth(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            event.args["from"], humanize_wei(event.args["amount"])
        ) + await self.notification_footer(event)

    @register_event("BondClaimedUnstETH")
    async def bond_claimed_unsteth(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            event.args["to"],
            humanize_wei(event.args["amount"]),
            event.args["requestId"],
        ) + await self.notification_footer(event)

    @register_event("BondClaimedStETH")
    async def bond_claimed_steth(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            event.args["to"], humanize_wei(event.args["amount"])
        ) + await self.notification_footer(event)

    @register_event("BondClaimedWstETH")
    async def bond_claimed_wsteth(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            event.args["to"], humanize_wei(event.args["amount"])
        ) + await self.notification_footer(event)

    @register_event("BondBurned")
    async def bond_burned(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(humanize_wei(event.args["burnedAmount"])) + await self.notification_footer(
            event
        )

    @register_event("BondCharged")
    async def bond_charged(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            humanize_wei(event.args["amountToCharge"]),
            humanize_wei(event.args["chargedAmount"]),
        ) + await self.notification_footer(event)

    @register_event("BondLockChanged")
    async def bond_lock_changed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        until = datetime.datetime.fromtimestamp(event.args["until"], datetime.UTC)
        return template(
            humanize_wei(event.args["newAmount"]), _format_date(until)
        ) + await self.notification_footer(event)

    @register_event("BondLockRemoved")
    async def bond_lock_removed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template() + await self.notification_footer(event)

    @register_event("BondLockCompensated")
    async def bond_lock_compensated(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(humanize_wei(event.args["amount"])) + await self.notification_footer(event)

    @register_event("BondLockPeriodChanged")
    async def bond_lock_period_changed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            str(datetime.timedelta(seconds=event.args["period"]))
        ) + await self.notification_footer(event)

    @register_event("NodeOperatorEffectiveWeightChanged")
    async def node_operator_effective_weight_changed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            event.args["oldWeight"], event.args["newWeight"]
        ) + await self.notification_footer(event)

    @register_event("OperatorGroupCreated")
    async def operator_group_created(self, event: EventNotification):
        template = self._require_message_template(event.event)
        group_info = event.args["groupInfo"]
        operator_ids = _sub_node_operator_ids(group_info)
        if not operator_ids:
            return None
        message = template(
            event.args["groupId"],
            await self._sub_node_operator_allocations(group_info, event.block),
            group_name=_operator_group_name(group_info),
        ) + await self.notification_footer(event)
        return NotificationPlan.broadcast_to_operators(message, operator_ids)

    @register_event("OperatorGroupUpdated")
    async def operator_group_updated(self, event: EventNotification):
        template = self._require_message_template(event.event)
        group_id = event.args["groupId"]
        previous_group = await self.meta_registry.functions.getOperatorGroup(group_id).call(
            block_identifier=event.block - 1
        )
        current_group = event.args["groupInfo"]
        previous_group_name = _operator_group_name(previous_group)
        current_group_name = _operator_group_name(current_group)
        previous_shares = _sub_node_operator_shares(previous_group)
        current_shares = _sub_node_operator_shares(current_group)
        changed_operator_ids = set(previous_shares) ^ set(current_shares)
        changed_operator_ids.update(
            node_operator_id
            for node_operator_id in set(previous_shares) & set(current_shares)
            if previous_shares[node_operator_id] != current_shares[node_operator_id]
        )
        is_renamed = previous_group_name != current_group_name
        if not changed_operator_ids:
            if not is_renamed:
                return None
            operator_ids = set(current_shares)
            if not operator_ids:
                return None
            message = template(
                group_id,
                change_kind="renamed",
                old_group_name=previous_group_name,
                new_group_name=current_group_name,
            ) + await self.notification_footer(event)
            return NotificationPlan.broadcast_to_operators(message, operator_ids)

        previous_operators = {
            int(read_field(operator, "nodeOperatorId", 0)): operator
            for operator in await self._sub_node_operator_allocations(
                previous_group, event.block - 1
            )
        }
        current_operators = {
            int(read_field(operator, "nodeOperatorId", 0)): operator
            for operator in await self._sub_node_operator_allocations(current_group, event.block)
        }
        target_operator_ids = changed_operator_ids | (set(current_shares) if is_renamed else set())
        footer = await self.notification_footer(event)
        operator_messages: dict[int, str] = {}
        for node_operator_id in target_operator_ids:
            if node_operator_id not in changed_operator_ids:
                message = template(
                    group_id,
                    change_kind="renamed",
                    old_group_name=previous_group_name,
                    new_group_name=current_group_name,
                )
            elif node_operator_id not in previous_shares:
                node_operator_label = await self._node_operator_label(node_operator_id, event.block)
                message = template(
                    group_id,
                    node_operator_label,
                    "added",
                    new_operator=current_operators[node_operator_id],
                    group_name=current_group_name,
                    old_group_name=previous_group_name if is_renamed else None,
                    new_group_name=current_group_name if is_renamed else None,
                )
            elif node_operator_id not in current_shares:
                node_operator_label = await self._node_operator_label(node_operator_id, event.block)
                message = template(
                    group_id,
                    node_operator_label,
                    "removed",
                    old_operator=previous_operators[node_operator_id],
                    group_name=current_group_name,
                    old_group_name=previous_group_name if is_renamed else None,
                    new_group_name=current_group_name if is_renamed else None,
                )
            else:
                node_operator_label = await self._node_operator_label(node_operator_id, event.block)
                message = template(
                    group_id,
                    node_operator_label,
                    "changed",
                    old_operator=previous_operators[node_operator_id],
                    new_operator=current_operators[node_operator_id],
                    group_name=current_group_name,
                    old_group_name=previous_group_name if is_renamed else None,
                    new_group_name=current_group_name if is_renamed else None,
                )
            operator_messages[node_operator_id] = f"{message}{footer}"
        return NotificationPlan.per_operator(operator_messages)

    @register_event("OperatorGroupCleared")
    async def operator_group_cleared(self, event: EventNotification):
        template = self._require_message_template(event.event)
        previous_group = await self.meta_registry.functions.getOperatorGroup(
            event.args["groupId"]
        ).call(block_identifier=event.block - 1)
        operator_ids = _sub_node_operator_ids(previous_group)
        if not operator_ids:
            return None
        message = template(
            event.args["groupId"],
            await self._sub_node_operator_labels(previous_group, event.block - 1),
            group_name=_operator_group_name(previous_group),
        ) + await self.notification_footer(event)
        return NotificationPlan.broadcast_to_operators(message, operator_ids)

    @register_event("BondCurveWeightSet")
    async def bond_curve_weight_set(self, event: EventNotification):
        template = self._require_message_template(event.event)
        message = template(event.args["curveId"], event.args["weight"])
        operator_ids = await self._node_operator_ids_for_bond_curve(
            event.args["curveId"], event.block
        )
        if not operator_ids:
            return None

        operator_messages: dict[int, str] = {}
        for node_operator_id in operator_ids:
            targeted_event = replace(
                event.primary_event, args=dict(event.args) | {"nodeOperatorId": node_operator_id}
            )
            operator_messages[node_operator_id] = (
                f"{message}{await self.event_footer(targeted_event)}"
            )
        return NotificationPlan.per_operator(operator_messages)

    @register_event("OperatorMetadataSet")
    async def operator_metadata_set(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(event.args["metadata"]) + await self.notification_footer(event)

    async def distribution_log_updated(self, event: EventNotification):
        template = self._require_message_template(event.event)
        base_message = template()
        footer = await self.notification_footer(event)
        fallback_message = f"{base_message}{footer}"

        log_cid = event.args.get("logCid")
        try:
            distribution_log = await self._fetch_distribution_log(log_cid)
        except Exception as exc:
            logger.warning(
                "Failed to enrich DistributionLogUpdated for logCid %s: %s",
                log_cid,
                exc,
            )
            return NotificationPlan.broadcast(fallback_message)

        summary = parse_distribution_log(distribution_log)

        operator_messages: dict[str, str] = {}
        for operator_id, flagged in summary.strikes_per_operator.items():
            flagged_sorted = sorted(flagged, key=lambda item: validator_sort_key(item[0]))
            node_operator_label = await self._node_operator_label(int(operator_id), event.block)
            operator_messages[str(operator_id)] = (
                f"{template(node_operator_label, flagged_sorted)}{footer}"
            )

        if not operator_messages:
            if summary.all_operator_ids:
                return NotificationPlan.broadcast_to_operators(
                    fallback_message,
                    summary.all_operator_ids,
                )
            return NotificationPlan.broadcast(fallback_message)

        def render(node_operator_ids: frozenset[str]) -> tuple[str, ...]:
            messages = tuple(
                operator_messages[node_operator_id]
                for node_operator_id in sorted(
                    node_operator_ids,
                    key=lambda value: int(value),
                )
                if node_operator_id in operator_messages
            )
            return messages or (fallback_message,)

        return NotificationPlan.per_chat(summary.all_operator_ids, render)

    async def _node_operator_ids_for_bond_curve(self, curve_id: int, block: int) -> set[int]:
        operators_count = await self.module.functions.getNodeOperatorsCount().call(
            block_identifier=block
        )
        operator_ids: set[int] = set()
        for node_operator_id in range(operators_count):
            operator_curve_id = await self.accounting.functions.getBondCurveId(
                node_operator_id
            ).call(block_identifier=block)
            if int(operator_curve_id) == int(curve_id):
                operator_ids.add(node_operator_id)
        return operator_ids


register_event(
    "DepositedSigningKeysCountChanged",
    digest_name=DigestGroups.DEPOSITED_SIGNING_KEYS,
)(CuratedEventMessages.deposited_signing_keys_count_changed)
register_event(
    "TotalSigningKeysCountChanged",
    aggregation_group=AggregationGroups.TOTAL_SIGNING_KEY_COUNTS,
)(CuratedEventMessages.total_signing_keys_count_changed)
register_event("VettedSigningKeysCountDecreased")(
    CuratedEventMessages.vetted_signing_keys_count_decreased
)
register_event("KeyRemovalChargeApplied")(CuratedEventMessages.key_removal_charge_applied)
register_event("KeyAllocatedBalanceChanged")(CuratedEventMessages.key_allocated_balance_changed)
register_event("BondCurveSet")(CuratedEventMessages.bond_curve_set)
register_event("NodeOperatorManagerAddressChangeProposed")(
    CuratedEventMessages.node_operator_manager_address_change_proposed
)
register_event("NodeOperatorManagerAddressChanged")(
    CuratedEventMessages.node_operator_manager_address_changed
)
register_event("NodeOperatorRewardAddressChangeProposed")(
    CuratedEventMessages.node_operator_reward_address_change_proposed
)
register_event("NodeOperatorRewardAddressChanged")(
    CuratedEventMessages.node_operator_reward_address_changed
)
register_event("CustomRewardsClaimerSet")(CuratedEventMessages.custom_rewards_claimer_set)
register_event("FeeSplitsSet")(CuratedEventMessages.fee_splits_set)
register_event("BondDebtIncreased")(CuratedEventMessages.bond_debt_increased)
register_event("BondDebtCovered")(CuratedEventMessages.bond_debt_covered)
register_event("GeneralDelayedPenaltyReported")(
    CuratedEventMessages.general_delayed_penalty_reported
)
register_event("GeneralDelayedPenaltySettled")(CuratedEventMessages.general_delayed_penalty_settled)
register_event("GeneralDelayedPenaltyCancelled")(
    CuratedEventMessages.general_delayed_penalty_cancelled
)
register_event("GeneralDelayedPenaltyCompensated")(
    CuratedEventMessages.general_delayed_penalty_compensated
)
register_event("ValidatorSlashingReported")(CuratedEventMessages.validator_slashing_reported)
register_event(
    "ValidatorExitRequest",
    aggregation_group=AggregationGroups.VALIDATOR_EXIT_REQUESTS,
)(CuratedEventMessages.validator_exit_request)
register_event("TriggeredExitFeeRecorded")(CuratedEventMessages.triggered_exit_fee_recorded)
register_event("StrikesPenaltyProcessed")(CuratedEventMessages.strikes_penalty_processed)
register_event(
    "ValidatorWithdrawn",
    aggregation_group=AggregationGroups.VALIDATOR_WITHDRAWALS,
)(CuratedEventMessages.validator_withdrawn)
register_event("DistributionLogUpdated")(CuratedEventMessages.distribution_log_updated)
