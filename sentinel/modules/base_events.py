import datetime
import logging
from typing import Any

from eth_utils import humanize_wei

from sentinel.models import Event, EventNotification
from sentinel.modules.distribution import (
    DistributionLogFetcher,
    distribution_report_rewards,
    parse_distribution_log,
    validator_sort_key,
)
from sentinel.modules.event_engine import EventMessageEngineBase
from sentinel.modules.formatting import block_footer_tx_only
from sentinel.notifications import NotificationPlan

logger = logging.getLogger(__name__)


def _format_date(date: datetime.datetime):
    return date.strftime("%a %d %b %Y, %I:%M%p UTC")


class BaseModule(EventMessageEngineBase):
    module: Any
    accounting: Any
    parametersRegistry: Any
    _distribution_log_fetcher: DistributionLogFetcher

    def _bind_module_adapter(self, module_adapter: Any) -> None:
        self.module_adapter = module_adapter
        self.chain = module_adapter.chain
        self.module_address = module_adapter.addresses.module
        self.accounting_address = module_adapter.addresses.accounting
        self.parameters_registry_address = module_adapter.addresses.parameters_registry
        self.module = module_adapter.contracts.module
        self.accounting = module_adapter.contracts.accounting
        self.parametersRegistry = module_adapter.contracts.parameters_registry

    async def deposited_signing_keys_count_changed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        events_by_operator: dict[int, list[Event]] = {}
        for source_event in event.source_events:
            node_operator_id = int(source_event.args["nodeOperatorId"])
            events_by_operator.setdefault(node_operator_id, []).append(source_event)

        entries_by_operator: dict[str, tuple[str, int, int]] = {}
        for node_operator_id, operator_events in sorted(events_by_operator.items()):
            first_event = operator_events[0]
            last_event = operator_events[-1]
            # The event contains post-state, so read the operator before its first digest event.
            node_operator = await self.module.functions.getNodeOperator(node_operator_id).call(
                block_identifier=first_event.block - 1
            )
            entries_by_operator[str(node_operator_id)] = (
                await self._digest_operator_label(node_operator_id, last_event.block),
                int(node_operator.totalDepositedKeys),
                int(last_event.args["depositedKeysCount"]),
            )

        footer = await self.block_range_footer(event)

        def render(node_operator_ids: frozenset[str]) -> tuple[str, ...]:
            entries = [
                entries_by_operator[node_operator_id]
                for node_operator_id in sorted(
                    node_operator_ids,
                    key=lambda value: int(value),
                )
            ]
            return (template(entries) + footer,)

        return NotificationPlan.per_chat(
            node_operator_ids=entries_by_operator,
            render=render,
        )

    async def total_signing_keys_count_changed(self, event: EventNotification):
        first_event = event.source_events[0]
        template = self._require_message_template(event.event)
        node_operator = await self.module.functions.getNodeOperator(
            event.args["nodeOperatorId"]
        ).call(block_identifier=first_event.block - 1)
        footer = await self.notification_footer(event)
        return template(event.args["totalKeysCount"], node_operator.totalAddedKeys) + footer

    async def vetted_signing_keys_count_decreased(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template() + await self.notification_footer(event)

    async def key_removal_charge_applied(self, event: EventNotification):
        template = self._require_message_template(event.event)
        curve_id = await self.accounting.functions.getBondCurveId(
            event.args["nodeOperatorId"]
        ).call(block_identifier=event.block)
        charge_per_key = await self.parametersRegistry.functions.getKeyRemovalCharge(curve_id).call(
            block_identifier=event.block
        )
        removed_key_logs = await self.module.events.SigningKeyRemoved().get_logs(
            from_block=event.block,
            to_block=event.block,
        )
        removed_keys_count = sum(
            log["transactionIndex"] == event.primary_event.transaction_index
            and log["args"]["nodeOperatorId"] == event.args["nodeOperatorId"]
            for log in removed_key_logs
        )
        if removed_keys_count == 0:
            logger.warning(
                "No SigningKeyRemoved logs found for key removal charge",
                extra={
                    "block": event.block,
                    "node_operator_id": event.args["nodeOperatorId"],
                    "transaction_hash": event.tx.hex(),
                },
            )
            return template(
                humanize_wei(charge_per_key),
                total=False,
            ) + await self.notification_footer(event)

        total_charge = charge_per_key * removed_keys_count
        return template(
            humanize_wei(total_charge),
            total=True,
        ) + await self.notification_footer(event)

    async def notification_footer(self, event: EventNotification) -> str:
        source_events = event.source_events
        source_txs = {source_event.tx for source_event in source_events}
        if len(source_txs) == 1:
            return await self.event_footer(source_events[-1])
        return await self.block_footer(event)

    @staticmethod
    def notification_block_range(event: EventNotification) -> tuple[int, int]:
        source_blocks = {source_event.block for source_event in event.source_events}
        return min(source_blocks), max(source_blocks)

    async def block_footer(self, event: EventNotification) -> str:
        raise NotImplementedError

    async def block_range_footer(self, event: EventNotification) -> str:
        start_block, end_block = self.notification_block_range(event)
        block_links = [(str(start_block), self.block_link(start_block))]
        if end_block != start_block:
            block_links.append((str(end_block), self.block_link(end_block)))
        return block_footer_tx_only(block_links).as_markdown()

    async def _digest_operator_label(self, node_operator_id: int, block: int) -> str:
        _ = block
        return f"#{node_operator_id}"

    async def key_allocated_balance_changed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            event.args["keyIndex"],
            humanize_wei(event.args["newTotal"]),
        ) + await self.notification_footer(event)

    async def bond_curve_set(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(event.args["curveId"]) + await self.notification_footer(event)

    async def node_operator_manager_address_change_proposed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(event.args["newProposedAddress"]) + await self.notification_footer(event)

    async def node_operator_manager_address_changed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(event.args["newAddress"]) + await self.notification_footer(event)

    async def node_operator_reward_address_change_proposed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(event.args["newProposedAddress"]) + await self.notification_footer(event)

    async def node_operator_reward_address_changed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(event.args["newAddress"]) + await self.notification_footer(event)

    async def custom_rewards_claimer_set(self, event: EventNotification):
        template = self._require_message_template(event.event)
        previous_rewards_claimer = await self.accounting.functions.getCustomRewardsClaimer(
            event.args["nodeOperatorId"]
        ).call(block_identifier=event.block - 1)
        return template(
            event.args["rewardsClaimer"], previous_rewards_claimer
        ) + await self.notification_footer(event)

    async def fee_splits_set(self, event: EventNotification):
        template = self._require_message_template(event.event)
        previous_fee_splits = await self.accounting.functions.getFeeSplits(
            event.args["nodeOperatorId"]
        ).call(block_identifier=event.block - 1)
        return template(
            event.args["feeSplits"], previous_fee_splits
        ) + await self.notification_footer(event)

    async def bond_debt_increased(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(humanize_wei(event.args["amount"])) + await self.notification_footer(event)

    async def bond_debt_covered(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(humanize_wei(event.args["amount"])) + await self.notification_footer(event)

    async def bond_lock_removed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template() + await self.notification_footer(event)

    async def general_delayed_penalty_reported(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(
            humanize_wei(event.args["amount"]),
            humanize_wei(event.args["additionalFine"]),
            event.args["details"],
        ) + await self.notification_footer(event)

    async def general_delayed_penalty_settled(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(humanize_wei(event.args["amount"])) + await self.notification_footer(event)

    async def general_delayed_penalty_cancelled(self, event: EventNotification):
        template = self._require_message_template(event.event)
        remaining_amount = humanize_wei(
            await self.accounting.functions.getActualLockedBond(event.args["nodeOperatorId"]).call(
                block_identifier=event.block
            )
        )
        return template(remaining_amount) + await self.notification_footer(event)

    async def general_delayed_penalty_compensated(self, event: EventNotification):
        template = self._require_message_template(event.event)
        return template(humanize_wei(event.args["amount"])) + await self.notification_footer(event)

    async def validator_slashing_reported(self, event: EventNotification):
        template = self._require_message_template(event.event)
        key, key_url = self.validator_link(event.args["pubkey"])
        return template(key, key_url, event.args["keyIndex"]) + await self.notification_footer(
            event
        )

    async def validator_exit_request(self, event: EventNotification):
        # TODO: add delayed reminders. Before reminding, verify each validator has not exited
        # yet; this needs persisted pending-exit state plus a scheduled recheck.
        template = self._require_message_template(event.event)
        curve_id = await self.accounting.functions.getBondCurveId(
            event.args["nodeOperatorId"]
        ).call(block_identifier=event.block)
        allowed_exit_delay = await self.parametersRegistry.functions.getAllowedExitDelay(
            curve_id
        ).call(block_identifier=event.block)
        exit_requests = []
        for source_event in event.source_events:
            key, key_url = self.validator_link(source_event.args["validatorPubkey"])
            request_date = datetime.datetime.fromtimestamp(
                source_event.args["timestamp"], datetime.UTC
            )
            exit_until = request_date + datetime.timedelta(seconds=allowed_exit_delay)
            exit_requests.append(
                {
                    "key": key,
                    "key_url": key_url,
                    "request_date": _format_date(request_date),
                    "exit_until": _format_date(exit_until),
                }
            )
        return template(exit_requests) + await self.notification_footer(event)

    async def triggered_exit_fee_recorded(self, event: EventNotification):
        template = self._require_message_template(event.event)
        key, key_url = self.validator_link(event.args["pubkey"])
        return template(
            key,
            key_url,
            humanize_wei(event.args["withdrawalRequestRecordedFee"]),
        ) + await self.notification_footer(event)

    async def strikes_penalty_processed(self, event: EventNotification):
        template = self._require_message_template(event.event)
        key, key_url = self.validator_link(event.args["pubkey"])
        return template(
            key, key_url, humanize_wei(event.args["strikesPenalty"])
        ) + await self.notification_footer(event)

    async def validator_withdrawn(self, event: EventNotification):
        template = self._require_message_template(event.event)
        withdrawals = []
        for source_event in event.source_events:
            key, key_url = self.validator_link(source_event.args["pubkey"])
            withdrawals.append(
                {
                    "key": key,
                    "key_url": key_url,
                    "balance": humanize_wei(source_event.args["exitBalance"]),
                    "slashing_penalty": humanize_wei(source_event.args["slashingPenalty"]),
                }
            )
        return template(withdrawals) + await self.notification_footer(event)

    async def distribution_log_updated(self, event: EventNotification):
        if distribution_report_rewards(event) == 0:
            return None

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
            operator_label = await self._distribution_operator_label(operator_id, event.block)
            operator_messages[str(operator_id)] = (
                f"{template(operator_label, flagged_sorted)}{footer}"
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

    async def _distribution_operator_label(self, operator_id: str, block: int) -> str:
        _ = block
        return operator_id
