from enum import StrEnum

from aiogram.utils.formatting import Bold, Code, Text, TextLink
from web3.constants import ADDRESS_ZERO

from sentinel.config import get_config
from sentinel.modules.catalog import EventDefinition, build_grouped_event_list_text
from sentinel.modules.formatting import (
    block_footer,
    block_footer_tx_only,
    markdown,
    nl,
    read_field,
    transaction_footer,
    transaction_footer_tx_only,
)
from sentinel.modules.registry import RegisterEventMessage
from sentinel.modules.texts import BotTexts


CURATED_EVENT_MESSAGES = {}


def register_event_message(event_name: str):
    return RegisterEventMessage(CURATED_EVENT_MESSAGES, event_name)


class EventGroup(StrEnum):
    KEY_MANAGEMENT = "Key Management Events:"
    ADDRESS_AND_REWARD_CHANGES = "Address and Reward Changes:"
    BOND = "Bond Events:"
    PENALTIES = "Penalty Events:"
    WITHDRAWAL_AND_EXIT = "Withdrawal and Exit Requests:"
    OPERATOR_GROUPS = "Operator Group Events:"
    COMMON_CURATED = "Common Curated Module Events:"


GROUP_DESCRIPTIONS: dict[EventGroup, str] = {
    EventGroup.KEY_MANAGEMENT: "Changes related to keys, limits, and operator type.",
    EventGroup.ADDRESS_AND_REWARD_CHANGES: "Changes or proposals for management and reward configuration.",
    EventGroup.BOND: "Changes to deposited, claimed, locked, burned, or charged bond.",
    EventGroup.PENALTIES: "Alerts for delayed penalties, slashing, debt, and strikes.",
    EventGroup.WITHDRAWAL_AND_EXIT: "Notifications for exit requests and confirmed withdrawals.",
    EventGroup.OPERATOR_GROUPS: "Changes from the Curated Module MetaRegistry.",
    EventGroup.COMMON_CURATED: "",
}


CURATED_EVENT_CATALOG: list[EventDefinition] = [
    EventDefinition(
        "DepositedSigningKeysCountChanged",
        "- 🤩 Node Operator's keys received deposits",
        EventGroup.KEY_MANAGEMENT,
    ),
    EventDefinition(
        "TotalSigningKeysCountChanged",
        "- 👀 New keys uploaded or removed",
        EventGroup.KEY_MANAGEMENT,
    ),
    EventDefinition(
        "VettedSigningKeysCountDecreased",
        "- 🚨 Invalid or duplicated keys has been uploaded",
        EventGroup.KEY_MANAGEMENT,
    ),
    EventDefinition(
        "KeyRemovalChargeApplied",
        "- 🔑 Applied charge for key removal",
        EventGroup.KEY_MANAGEMENT,
    ),
    EventDefinition(
        "KeyAllocatedBalanceChanged",
        "- 👀 Key balance increased",
        EventGroup.KEY_MANAGEMENT,
    ),
    EventDefinition(
        "BondCurveSet",
        "- ℹ️ Node Operator type changed",
        EventGroup.KEY_MANAGEMENT,
    ),
    EventDefinition(
        "TargetValidatorsCountChanged",
        "- 🚨 Target validators count changed",
        EventGroup.KEY_MANAGEMENT,
    ),
    EventDefinition(
        "NodeOperatorManagerAddressChangeProposed",
        "- ℹ️ New manager address proposed",
        EventGroup.ADDRESS_AND_REWARD_CHANGES,
    ),
    EventDefinition(
        "NodeOperatorManagerAddressChanged",
        "- ✅ Manager address changed",
        EventGroup.ADDRESS_AND_REWARD_CHANGES,
    ),
    EventDefinition(
        "NodeOperatorRewardAddressChangeProposed",
        "- ℹ️ New rewards address proposed",
        EventGroup.ADDRESS_AND_REWARD_CHANGES,
    ),
    EventDefinition(
        "NodeOperatorRewardAddressChanged",
        "- ✅ Rewards address changed",
        EventGroup.ADDRESS_AND_REWARD_CHANGES,
    ),
    EventDefinition(
        "CustomRewardsClaimerSet",
        "- ℹ️ Custom rewards claimer changed",
        EventGroup.ADDRESS_AND_REWARD_CHANGES,
    ),
    EventDefinition(
        "FeeSplitsSet",
        "- ℹ️ Fee splits changed",
        EventGroup.ADDRESS_AND_REWARD_CHANGES,
    ),
    EventDefinition("BondDepositedETH", "- ✅ ETH bond deposited", EventGroup.BOND),
    EventDefinition("BondDepositedStETH", "- ✅ stETH bond deposited", EventGroup.BOND),
    EventDefinition("BondDepositedWstETH", "- ✅ wstETH bond deposited", EventGroup.BOND),
    EventDefinition("BondClaimedUnstETH", "- ✅ unstETH bond claim requested", EventGroup.BOND),
    EventDefinition("BondClaimedStETH", "- ✅ stETH bond claimed", EventGroup.BOND),
    EventDefinition("BondClaimedWstETH", "- ✅ wstETH bond claimed", EventGroup.BOND),
    EventDefinition("BondBurned", "- 🚨 Bond burned", EventGroup.BOND),
    EventDefinition("BondCharged", "- 🚨 Bond charged", EventGroup.BOND),
    EventDefinition("BondLockChanged", "- 🚨 Bond lock changed", EventGroup.BOND),
    EventDefinition("BondLockRemoved", "- ✅ Bond lock removed", EventGroup.BOND),
    EventDefinition("BondLockCompensated", "- ✅ Bond lock compensated", EventGroup.BOND),
    EventDefinition("BondLockPeriodChanged", "- ℹ️ Bond lock period changed", EventGroup.BOND),
    EventDefinition(
        "GeneralDelayedPenaltyReported",
        "- 🚨 General delayed penalty reported",
        EventGroup.PENALTIES,
    ),
    EventDefinition(
        "GeneralDelayedPenaltySettled",
        "- 🚨 General delayed penalty confirmed and applied",
        EventGroup.PENALTIES,
    ),
    EventDefinition(
        "GeneralDelayedPenaltyCancelled",
        "- 😮‍💨 Cancelled general delayed penalty",
        EventGroup.PENALTIES,
    ),
    EventDefinition(
        "GeneralDelayedPenaltyCompensated",
        "- ✅ General delayed penalty compensated",
        EventGroup.PENALTIES,
    ),
    EventDefinition(
        "ValidatorSlashingReported", "- 🚨 Validator slashing reported", EventGroup.PENALTIES
    ),
    EventDefinition("BondDebtIncreased", "- 🚨 Bond debt increased", EventGroup.PENALTIES),
    EventDefinition("BondDebtCovered", "- ✅ Bond debt covered", EventGroup.PENALTIES),
    EventDefinition(
        "StrikesPenaltyProcessed", "- 🚨 Strikes penalty processed", EventGroup.PENALTIES
    ),
    EventDefinition(
        "ValidatorExitRequest",
        "- 🚨 One of the validators requested to exit",
        EventGroup.WITHDRAWAL_AND_EXIT,
    ),
    EventDefinition(
        "ValidatorExitDelayProcessed",
        "- 🚨 Validator exit delay penalty issued",
        EventGroup.WITHDRAWAL_AND_EXIT,
    ),
    EventDefinition(
        "TriggeredExitFeeRecorded",
        "- 🚨 Triggerable Withdrawal fee recorded",
        EventGroup.WITHDRAWAL_AND_EXIT,
    ),
    EventDefinition(
        "ValidatorWithdrawn",
        "- 👀 Validator withdrawal confirmed",
        EventGroup.WITHDRAWAL_AND_EXIT,
    ),
    EventDefinition(
        "NodeOperatorEffectiveWeightChanged",
        "- ℹ️ Operator effective weight changed",
        EventGroup.OPERATOR_GROUPS,
    ),
    EventDefinition(
        "OperatorGroupCreated", "- ℹ️ Operator group created", EventGroup.OPERATOR_GROUPS
    ),
    EventDefinition(
        "OperatorGroupUpdated", "- ℹ️ Operator group updated", EventGroup.OPERATOR_GROUPS
    ),
    EventDefinition(
        "OperatorGroupCleared", "- 🚨 Operator group cleared", EventGroup.OPERATOR_GROUPS
    ),
    EventDefinition(
        "BondCurveWeightSet", "- ℹ️ Operator type weight changed", EventGroup.OPERATOR_GROUPS
    ),
    EventDefinition(
        "OperatorMetadataSet", "- ℹ️ Operator metadata changed", EventGroup.OPERATOR_GROUPS
    ),
    EventDefinition(
        "DistributionLogUpdated", "- 📈 New rewards distributed", EventGroup.COMMON_CURATED
    ),
    EventDefinition("Resumed", "- 🎉 Curated Module launched", EventGroup.COMMON_CURATED),
]

CURATED_EVENT_DESCRIPTIONS = {event.name: event.description for event in CURATED_EVENT_CATALOG}


def build_event_list_text(catalog_events: set[str], module_ui_url: str | None = None) -> str:
    _ = module_ui_url
    return build_grouped_event_list_text(
        catalog_events=catalog_events,
        catalog=CURATED_EVENT_CATALOG,
        group_descriptions=GROUP_DESCRIPTIONS,
    )


WELCOME_TEXT = (
    "Welcome to the Curated Module Sentinel! "
    + nl()
    + "Here you can follow Node Operators and receive notifications about their events."
    + nl()
    + "To get started, please use the buttons below."
    + nl()
)
START_BUTTON_FOLLOW = "Follow"
START_BUTTON_UNFOLLOW = "Unfollow"
START_BUTTON_EVENTS = "Events"
BUTTON_BACK = "Back"
START_BUTTON_ADMIN = "Admin"
ADMIN_BUTTON_SUBSCRIPTIONS = "Subscriptions"
ADMIN_MENU_TEXT = "Admin tools:"
ADMIN_BUTTON_BROADCAST = "Broadcast"
ADMIN_BROADCAST_MENU_TEXT = "Choose broadcast target:"
ADMIN_BROADCAST_ALL = "All subscribers"
ADMIN_BROADCAST_BY_NO = "By node operator"
ADMIN_BROADCAST_ENTER_MESSAGE_ALL = "Please enter the message to send to all subscribers:"
ADMIN_BROADCAST_ENTER_NO_IDS = "Please enter comma-separated node operator IDs (e.g., 1,2,3):"
ADMIN_BROADCAST_NO_IDS_INVALID = "No valid node operator IDs provided."
ADMIN_BROADCAST_CONFIRM_HINT = "Review the message below and confirm before broadcasting."
ADMIN_BROADCAST_PREVIEW_ALL = "Broadcast preview for all subscribers:"
ADMIN_BROADCAST_PREVIEW_SELECTED = "Broadcast preview for: {targets}"
BUTTON_SEND_BROADCAST = "Send broadcast"
ADMIN_PRIVATE_CHAT_REQUIRED = (
    "Admin tools are only available in a private chat with the bot. "
    "Please open a private chat to continue."
)
NO_NEW_BLOCKS_ADMIN_ALERT = (
    "⚠️ No new blocks processed in the last {minutes} minutes. Latest block: {block}"
)
FOLLOW_NODE_OPERATOR_TEXT = (
    "Choose a Curated Node Operator below, or enter one or more Node Operator IDs "
    "separated by commas:"
)
FOLLOW_NODE_OPERATOR_FOLLOWING = "Node Operators you are following:\n{}\n\n"
UNFOLLOW_NODE_OPERATOR_TEXT = (
    "Choose a Curated Node Operator below, or enter one or more Node Operator IDs "
    "separated by commas:"
)
UNFOLLOW_NODE_OPERATOR_NOT_FOLLOWING = "You are not following any Node Operators."
UNFOLLOW_NODE_OPERATOR_FOLLOWING = "Node Operators you are following:\n{}\n\n"
NODE_OPERATOR_FOLLOWED = "You are now following Node Operator #{}"
NODE_OPERATOR_CANT_FOLLOW = "Invalid Node Operator id. Please enter the correct id."
NODE_OPERATOR_UNFOLLOWED = "You are no longer following Node Operator #{}"
NODE_OPERATOR_CANT_UNFOLLOW = (
    "Can't unfollow the Node Operator you are not following. Please enter the correct id."
)


CuratedTexts = BotTexts(
    WELCOME_TEXT=WELCOME_TEXT,
    START_BUTTON_FOLLOW=START_BUTTON_FOLLOW,
    START_BUTTON_UNFOLLOW=START_BUTTON_UNFOLLOW,
    START_BUTTON_EVENTS=START_BUTTON_EVENTS,
    BUTTON_BACK=BUTTON_BACK,
    START_BUTTON_ADMIN=START_BUTTON_ADMIN,
    ADMIN_BUTTON_SUBSCRIPTIONS=ADMIN_BUTTON_SUBSCRIPTIONS,
    ADMIN_MENU_TEXT=ADMIN_MENU_TEXT,
    ADMIN_BUTTON_BROADCAST=ADMIN_BUTTON_BROADCAST,
    ADMIN_BROADCAST_MENU_TEXT=ADMIN_BROADCAST_MENU_TEXT,
    ADMIN_BROADCAST_ALL=ADMIN_BROADCAST_ALL,
    ADMIN_BROADCAST_BY_NO=ADMIN_BROADCAST_BY_NO,
    ADMIN_BROADCAST_ENTER_MESSAGE_ALL=ADMIN_BROADCAST_ENTER_MESSAGE_ALL,
    ADMIN_BROADCAST_ENTER_NO_IDS=ADMIN_BROADCAST_ENTER_NO_IDS,
    ADMIN_BROADCAST_NO_IDS_INVALID=ADMIN_BROADCAST_NO_IDS_INVALID,
    ADMIN_BROADCAST_CONFIRM_HINT=ADMIN_BROADCAST_CONFIRM_HINT,
    ADMIN_BROADCAST_PREVIEW_ALL=ADMIN_BROADCAST_PREVIEW_ALL,
    ADMIN_BROADCAST_PREVIEW_SELECTED=ADMIN_BROADCAST_PREVIEW_SELECTED,
    BUTTON_SEND_BROADCAST=BUTTON_SEND_BROADCAST,
    ADMIN_PRIVATE_CHAT_REQUIRED=ADMIN_PRIVATE_CHAT_REQUIRED,
    NO_NEW_BLOCKS_ADMIN_ALERT=NO_NEW_BLOCKS_ADMIN_ALERT,
    FOLLOW_NODE_OPERATOR_TEXT=FOLLOW_NODE_OPERATOR_TEXT,
    FOLLOW_NODE_OPERATOR_FOLLOWING=FOLLOW_NODE_OPERATOR_FOLLOWING,
    UNFOLLOW_NODE_OPERATOR_TEXT=UNFOLLOW_NODE_OPERATOR_TEXT,
    UNFOLLOW_NODE_OPERATOR_NOT_FOLLOWING=UNFOLLOW_NODE_OPERATOR_NOT_FOLLOWING,
    UNFOLLOW_NODE_OPERATOR_FOLLOWING=UNFOLLOW_NODE_OPERATOR_FOLLOWING,
    NODE_OPERATOR_FOLLOWED=NODE_OPERATOR_FOLLOWED,
    NODE_OPERATOR_CANT_FOLLOW=NODE_OPERATOR_CANT_FOLLOW,
    NODE_OPERATOR_UNFOLLOWED=NODE_OPERATOR_UNFOLLOWED,
    NODE_OPERATOR_CANT_UNFOLLOW=NODE_OPERATOR_CANT_UNFOLLOW,
    build_event_list_text=build_event_list_text,
)


EVENT_EMITS = "Event {} emitted with data: \n{}"


def event_transaction_footer(no_id, tx_link: str) -> Text:
    return transaction_footer(f"nodeOperatorId: {no_id}", tx_link)


def event_transaction_footer_with_operator_name(no_id, name, tx_link: str) -> Text:
    return transaction_footer(f"Node Operator: #{no_id} - {name}", tx_link)


def event_transaction_footer_tx_only(tx_link: str) -> Text:
    return transaction_footer_tx_only(tx_link)


def event_block_footer(no_id, block_links: list[tuple[str, str]]) -> Text:
    return block_footer(f"nodeOperatorId: {no_id}", block_links)


def event_block_footer_with_operator_name(no_id, name, block_links: list[tuple[str, str]]) -> Text:
    return block_footer(f"Node Operator: #{no_id} - {name}", block_links)


def event_block_footer_tx_only(block_links: list[tuple[str, str]]) -> Text:
    return block_footer_tx_only(block_links)


@register_event_message("DepositedSigningKeysCountChanged")
def deposited_signing_keys_count_changed(entries: list[tuple[str, int, int]]):
    lines = []
    for label, count_before, count in entries:
        if lines:
            lines.append(nl(1))
        lines.extend(
            (
                "- ",
                label,
                ": ",
                Code(f"{count_before} -> {count}"),
            )
        )
    return markdown(
        "🤩 ",
        Bold("Deposit digest"),
        nl(),
        "Node Operators:",
        nl(1),
        *lines,
    )


@register_event_message("TotalSigningKeysCountChanged")
def total_signing_keys_count_changed(count, count_before):
    if count > count_before:
        return markdown(
            "👀 ",
            Bold("New keys uploaded"),
            nl(),
            "Keys count: ",
            Code(f"{count_before} -> {count}"),
        )
    return markdown("🚨 ", Bold("Key removed"), nl(), "Total keys: ", Code(count))


@register_event_message("VettedSigningKeysCountDecreased")
def vetted_signing_keys_count_decreased():
    cfg = get_config()
    return markdown(
        "🚨 ",
        Bold("Invalid or duplicated keys has been uploaded"),
        nl(),
        "Remove the keys on the ",
        TextLink("Curated Module UI", url=cfg.module_ui_url or ""),
        ".",
    )


@register_event_message("KeyRemovalChargeApplied")
def key_removal_charge_applied(amount, *, total: bool):
    label = "Amount of charge: " if total else "Charge per key: "
    return markdown("🔑 ", Bold("Key removal charge applied"), nl(), label, Code(amount))


@register_event_message("KeyAllocatedBalanceChanged")
def key_allocated_balance_changed(key_index, new_total):
    return markdown(
        "👀 ",
        Bold("Key balance increased"),
        nl(),
        "Key index: ",
        Code(str(key_index)),
        nl(1),
        "New allocated balance: ",
        Code(new_total),
    )


@register_event_message("BondCurveSet")
def bond_curve_set(curve_id: int):
    cfg = get_config()
    return markdown(
        "ℹ️ ",
        Bold("Operator type changed"),
        nl(),
        "New type id: ",
        Code(str(curve_id)),
        nl(1),
        "Operational requirements may now differ. Check the ",
        TextLink("Curated Module UI", url=cfg.module_ui_url or ""),
        " for updated guidance.",
    )


@register_event_message("TargetValidatorsCountChanged")
def target_validators_count_changed(
    mode_before: int,
    limit_before: int,
    mode_after: int,
    limit_after: int,
    active_validators_count: int,
):
    keys_above_limit = max(active_validators_count - limit_after, 0)
    key_word = "key" if keys_above_limit == 1 else "keys"
    match (mode_before, limit_before, mode_after, limit_after):
        case (_, _, 1, 0):
            return markdown(
                "🚨 ",
                Bold("Target validators count changed"),
                nl(),
                "The limit has been set to zero.",
                nl(1),
                f"{keys_above_limit} {key_word} will be requested to exit first.",
            )
        case (_, _, 2, 0):
            return markdown(
                "🚨 ",
                Bold("Target validators count changed"),
                nl(),
                "The limit has been set to zero.",
                nl(1),
                f"{keys_above_limit} {key_word} will be requested to exit immediately.",
            )
        case (1, _, 1, limit_after) if limit_after < limit_before:
            return markdown(
                "🚨 ",
                Bold("Target validators count changed"),
                nl(),
                f"The limit has been decreased from {limit_before} to {limit_after}.",
                nl(1),
                f"{keys_above_limit} {key_word} above the limit will be requested to exit first.",
            )
        case (2, _, 2, limit_after) if limit_after < limit_before:
            return markdown(
                "🚨 ",
                Bold("Target validators count changed"),
                nl(),
                f"The limit has been decreased from {limit_before} to {limit_after}.",
                nl(1),
                f"{keys_above_limit} {key_word} above the limit will be requested to exit immediately.",
            )
        case (_, _, 1, _):
            return markdown(
                "🚨 ",
                Bold("Target validators count changed"),
                nl(),
                f"The limit has been set to {limit_after}.",
                nl(1),
                f"{keys_above_limit} {key_word} above the limit will be requested to exit first.",
            )
        case (_, _, 2, _):
            return markdown(
                "🚨 ",
                Bold("Target validators count changed"),
                nl(),
                f"The limit has been set to {limit_after}.",
                nl(1),
                f"{keys_above_limit} {key_word} above the limit will be requested to exit immediately.",
            )
        case (2, limit_before, 0, _):
            return markdown(
                "✅ ",
                Bold("Unsetting forced exit target limit"),
                nl(),
                "The forced target validator limit of ",
                Code(str(limit_before)),
                " has been removed.",
                nl(1),
                "No additional validators will be requested to exit.",
            )
        case (_, _, 0, _):
            return markdown(
                "🚨 ",
                Bold("Target validators count changed"),
                nl(),
                "The limit has been set to zero. No keys will be requested to exit.",
            )
        case _:
            return markdown(
                "🚨 ",
                Bold("Target validators count changed"),
                nl(),
                f"Mode changed from {mode_before} to {mode_after}.",
                nl(1),
                f"Limit changed from {limit_before} to {limit_after}.",
            )


@register_event_message("NodeOperatorManagerAddressChangeProposed")
def node_operator_manager_address_change_proposed(address):
    if address == ADDRESS_ZERO:
        return markdown("ℹ️ ", Bold("Proposed manager address revoked"))
    return markdown(
        "ℹ️ ",
        Bold("New manager address proposed"),
        nl(),
        "Proposed address: ",
        Code(address),
        nl(),
        "To complete the change, the Node Operator must confirm it from the new address.",
    )


@register_event_message("NodeOperatorManagerAddressChanged")
def node_operator_manager_address_changed(address):
    return markdown("✅ ", Bold("Manager address changed"), nl(), "New address: ", Code(address))


@register_event_message("NodeOperatorRewardAddressChangeProposed")
def node_operator_reward_address_change_proposed(address):
    if address == ADDRESS_ZERO:
        return markdown("ℹ️ ", Bold("Proposed reward address revoked"))
    return markdown(
        "ℹ️ ",
        Bold("New rewards address proposed"),
        nl(),
        "Proposed address: ",
        Code(address),
        nl(),
        "To complete the change, the Node Operator must confirm it from the new address.",
    )


@register_event_message("NodeOperatorRewardAddressChanged")
def node_operator_reward_address_changed(address):
    return markdown("✅ ", Bold("Rewards address changed"), nl(), "New address: ", Code(address))


@register_event_message("CustomRewardsClaimerSet")
def custom_rewards_claimer_set(rewards_claimer, previous_rewards_claimer=None):
    if rewards_claimer == ADDRESS_ZERO:
        return markdown(
            "ℹ️ ",
            Bold("Custom rewards claimer removed"),
            nl(),
            "Custom rewards claimer was removed for this Node Operator.",
        )

    title = (
        "Custom rewards claimer set"
        if previous_rewards_claimer == ADDRESS_ZERO
        else "Custom rewards claimer changed"
    )
    return markdown(
        "ℹ️ ",
        Bold(title),
        nl(),
        "Rewards claimer: ",
        Code(rewards_claimer),
    )


def _format_fee_splits(fee_splits) -> str | Text:
    if not fee_splits:
        return "none"
    parts = []
    for fee_split in fee_splits:
        if parts:
            parts.append(nl(1))
        parts.extend(
            [
                f"- {_format_basis_points_percent(read_field(fee_split, 'share', 1))}: ",
                Code(read_field(fee_split, "recipient", 0)),
            ]
        )
    return Text(*parts)


def _fee_splits_title(fee_splits, previous_fee_splits) -> str:
    if fee_splits and not previous_fee_splits:
        return "Fee splits set"
    if not fee_splits and previous_fee_splits:
        return "Fee splits removed"
    return "Fee splits changed"


@register_event_message("FeeSplitsSet")
def fee_splits_set(fee_splits, previous_fee_splits=None):
    cfg = get_config()
    previous_fee_splits = previous_fee_splits or []
    parts: list = [
        "ℹ️ ",
        Bold(_fee_splits_title(fee_splits, previous_fee_splits)),
        nl(),
    ]
    if previous_fee_splits:
        parts.extend(
            [
                "Previous fee splits:",
                nl(1),
                _format_fee_splits(previous_fee_splits),
                nl(),
            ]
        )
    if fee_splits:
        parts.extend(["Fee splits:", nl(1), _format_fee_splits(fee_splits), nl()])
    if not fee_splits and not previous_fee_splits:
        parts.extend(["Fee splits: ", Code("none"), nl(1)])
    parts.extend(
        [
            "Review the current rewards setup in the ",
            TextLink("Curated Module UI", url=cfg.module_ui_url or ""),
            ".",
        ]
    )
    return markdown(
        *parts,
    )


@register_event_message("BondDebtIncreased")
def bond_debt_increased(amount):
    return markdown(
        "🚨 ",
        Bold("Bond debt increased"),
        nl(),
        "Debt increase: ",
        Code(amount),
        nl(),
        "Future rewards or bond changes may be used to cover this debt.",
    )


@register_event_message("BondDebtCovered")
def bond_debt_covered(amount):
    return markdown("✅ ", Bold("Bond debt covered"), nl(), "Covered amount: ", Code(amount))


@register_event_message("GeneralDelayedPenaltyReported")
def general_delayed_penalty_reported(amount, additional_fine, details):
    return markdown(
        "🚨 ",
        Bold("General delayed penalty reported"),
        nl(),
        "Penalty amount: ",
        Code(amount),
        nl(1),
        "Additional fine: ",
        Code(additional_fine),
        nl(1),
        "Details: ",
        Code(details),
    )


@register_event_message("GeneralDelayedPenaltySettled")
def general_delayed_penalty_settled(amount):
    return markdown(
        "🚨 ",
        Bold("General delayed penalty confirmed and applied"),
        nl(),
        "Settled amount: ",
        Code(amount),
    )


@register_event_message("GeneralDelayedPenaltyCancelled")
def general_delayed_penalty_cancelled(remaining):
    return markdown(
        "😮‍💨 ",
        Bold("General delayed penalty cancelled"),
        nl(),
        "Remaining amount: ",
        Code(remaining),
    )


@register_event_message("GeneralDelayedPenaltyCompensated")
def general_delayed_penalty_compensated(amount):
    return markdown(
        "✅ ",
        Bold("General delayed penalty compensated"),
        nl(),
        "Compensated amount: ",
        Code(amount),
    )


@register_event_message("ValidatorSlashingReported")
def validator_slashing_reported(key, key_url, key_index):
    return markdown(
        "🚨 ",
        Bold("Validator slashing reported"),
        nl(),
        "Validator: ",
        TextLink(key, url=key_url),
        nl(1),
        "Key index: ",
        Code(str(key_index)),
    )


@register_event_message("ValidatorExitRequest")
def validator_exit_request(exit_requests):
    if len(exit_requests) == 1:
        exit_request = exit_requests[0]
        return markdown(
            "🚨 ",
            Bold("Validator exit requested"),
            nl(),
            "Make sure to exit the key before ",
            exit_request["exit_until"],
            nl(1),
            "Requested key: ",
            TextLink(exit_request["key"], url=exit_request["key_url"]),
            nl(1),
            "Request date: ",
            Code(exit_request["request_date"]),
        )

    exit_until_values = {exit_request["exit_until"] for exit_request in exit_requests}
    request_date_values = {exit_request["request_date"] for exit_request in exit_requests}
    parts: list = [
        "🚨 ",
        Bold("Validator exits requested"),
        nl(),
    ]
    if len(exit_until_values) == 1:
        parts.extend(["Make sure to exit these keys before ", next(iter(exit_until_values)), nl(1)])
    parts.extend(
        [
            "Requested keys:",
            nl(1),
            _format_validator_exit_requests(
                exit_requests,
                include_exit_until=len(exit_until_values) > 1,
                include_request_date=len(request_date_values) > 1,
            ),
        ]
    )
    if len(request_date_values) == 1:
        parts.extend([nl(), "Request date: ", Code(next(iter(request_date_values)))])
    return markdown(*parts)


def _format_validator_exit_requests(
    exit_requests,
    *,
    include_exit_until: bool,
    include_request_date: bool,
) -> Text:
    parts = []
    for index, exit_request in enumerate(exit_requests, start=1):
        if parts:
            parts.append(nl(1))
        short_key = _shorten_validator_key_for_link(exit_request["key"])
        parts.extend(
            [
                "- ",
                "Validator ",
                str(index),
                ": ",
                TextLink(short_key, url=exit_request["key_url"]),
            ]
        )
        if include_exit_until:
            parts.extend([nl(1), "  Exit before: ", exit_request["exit_until"]])
        if include_request_date:
            parts.extend([nl(1), "  Request date: ", Code(exit_request["request_date"])])
    return Text(*parts)


def _shorten_validator_key_for_link(key: str) -> str:
    if len(key) <= 22:
        return key
    return f"{key[:10]}...{key[-8:]}"


@register_event_message("ValidatorExitDelayProcessed")
def validator_exit_delay_processed(key, key_url, penalty):
    return markdown(
        "🚨 ",
        Bold("Validator exit delay penalty issued"),
        nl(),
        "Validator: ",
        TextLink(key, url=key_url),
        nl(1),
        "Delay penalty: ",
        Code(penalty),
        nl(),
        "Penalty will be applied when the validator exits.",
    )


@register_event_message("TriggeredExitFeeRecorded")
def triggered_exit_fee_recorded(key, key_url, recorded_fee):
    return markdown(
        "🚨 ",
        Bold("Triggerable Withdrawal fee recorded"),
        nl(),
        "Validator: ",
        TextLink(key, url=key_url),
        nl(1),
        "Fee to be charged on exit: ",
        Code(recorded_fee),
        nl(),
        "Exit fee will be applied when the validator exits.",
    )


@register_event_message("StrikesPenaltyProcessed")
def strikes_penalty_processed(key, key_url, penalty):
    return markdown(
        "🚨 ",
        Bold("Strikes penalty processed"),
        nl(),
        "Validator: ",
        TextLink(key, url=key_url),
        nl(1),
        "Penalty amount: ",
        Code(penalty),
        nl(),
        "Penalty will be charged when the validator withdraws.",
    )


@register_event_message("ValidatorWithdrawn")
def validator_withdrawn(withdrawals):
    if len(withdrawals) == 1:
        withdrawal = withdrawals[0]
        parts: list = [
            "👀 ",
            Bold("Validator withdrawal confirmed"),
            nl(),
            "Withdrawn key: ",
            TextLink(withdrawal["key"], url=withdrawal["key_url"]),
            nl(1),
            "Exit balance: ",
            Code(withdrawal["balance"]),
        ]
        if withdrawal["slashing_penalty"] not in {"0 ether", "0 wei"}:
            parts.extend([nl(1), "Slashing penalty: ", Code(withdrawal["slashing_penalty"])])
        return markdown(*parts)

    parts: list = [
        "👀 ",
        Bold("Validator withdrawals confirmed"),
        nl(),
        "Withdrawn validators:",
        nl(1),
    ]
    for index, withdrawal in enumerate(withdrawals, start=1):
        if index > 1:
            parts.append(nl(1))
        parts.extend(
            [
                "- Validator ",
                str(index),
                ": ",
                TextLink(
                    _shorten_validator_key_for_link(withdrawal["key"]),
                    url=withdrawal["key_url"],
                ),
                nl(1),
                "  Exit balance: ",
                Code(withdrawal["balance"]),
            ]
        )
        if withdrawal["slashing_penalty"] not in {"0 ether", "0 wei"}:
            parts.extend(
                [
                    nl(1),
                    "  Slashing penalty: ",
                    Code(withdrawal["slashing_penalty"]),
                ]
            )
    return markdown(*parts)


@register_event_message("DistributionLogUpdated")
def distribution_data_updated(
    node_operator_label: str | None = None, striked_validators: list | None = None
):
    cfg = get_config()
    base_message = Text(
        "📈 ",
        Bold("Rewards distributed!"),
        nl(),
        "Follow the ",
        TextLink("Curated Module UI", url=cfg.module_ui_url or ""),
        " to check new claimable rewards.",
    )

    if node_operator_label is not None and striked_validators:
        return Text(
            base_message,
            Text(
                nl(),
                "⚠️ ",
                Bold("Strikes detected for validators"),
                nl(),
                "Node Operator: ",
                Code(str(node_operator_label)),
                nl(1),
                "Validators with strikes: ",
                Code(len(striked_validators)),
                nl(1),
            ),
        ).as_markdown()

    return base_message.as_markdown()


# TODO: Remove the temporary release notification after the CMv2 launch.
@register_event_message("Resumed")
def resumed():
    cfg = get_config()
    return markdown(
        "🎉 ",
        Bold("Curated Module is live!"),
        nl(),
        "Check the ",
        TextLink("Curated Module UI", url=cfg.module_ui_url or ""),
        " for operator workflows and current module details.",
    )


def _asset_amount(amount: str, asset: str) -> str:
    if amount.endswith(" ether"):
        return f"{amount.removesuffix(' ether')} {asset}"
    return f"{amount} {asset}"


def _bond_deposited(asset: str, sender: str, amount: str):
    return markdown(
        "✅ ",
        Bold("Bond deposited"),
        nl(),
        "From: ",
        Code(sender),
        nl(1),
        "Amount: ",
        Code(_asset_amount(amount, asset)),
    )


@register_event_message("BondDepositedETH")
def bond_deposited_eth(sender, amount):
    return _bond_deposited("ETH", sender, amount)


@register_event_message("BondDepositedStETH")
def bond_deposited_steth(sender, amount):
    return _bond_deposited("stETH", sender, amount)


@register_event_message("BondDepositedWstETH")
def bond_deposited_wsteth(sender, amount):
    return _bond_deposited("wstETH", sender, amount)


@register_event_message("BondClaimedUnstETH")
def bond_claimed_unsteth(recipient, amount, request_id):
    return markdown(
        "✅ ",
        Bold("Bond claim requested"),
        nl(),
        "Recipient: ",
        Code(recipient),
        nl(1),
        "Amount: ",
        Code(_asset_amount(amount, "unstETH")),
        nl(1),
        "Withdrawal request id: ",
        Code(str(request_id)),
    )


def _bond_claimed(asset: str, recipient: str, amount: str):
    return markdown(
        "✅ ",
        Bold("Bond claimed"),
        nl(),
        "Recipient: ",
        Code(recipient),
        nl(1),
        "Amount: ",
        Code(_asset_amount(amount, asset)),
    )


@register_event_message("BondClaimedStETH")
def bond_claimed_steth(recipient, amount):
    return _bond_claimed("stETH", recipient, amount)


@register_event_message("BondClaimedWstETH")
def bond_claimed_wsteth(recipient, amount):
    return _bond_claimed("wstETH", recipient, amount)


@register_event_message("BondBurned")
def bond_burned(amount):
    return markdown(
        "🚨 ", Bold("Bond burned"), nl(), "Burned amount: ", Code(_asset_amount(amount, "ETH"))
    )


@register_event_message("BondCharged")
def bond_charged(amount_to_charge, charged_amount):
    return markdown(
        "🚨 ",
        Bold("Bond charged"),
        nl(),
        "Requested charge: ",
        Code(_asset_amount(amount_to_charge, "ETH")),
        nl(1),
        "Charged amount: ",
        Code(_asset_amount(charged_amount, "ETH")),
    )


@register_event_message("BondLockChanged")
def bond_lock_changed(new_amount, until):
    return markdown(
        "🚨 ",
        Bold("Bond lock changed"),
        nl(),
        "Locked amount: ",
        Code(_asset_amount(new_amount, "ETH")),
        nl(1),
        "Locked until: ",
        Code(until),
    )


@register_event_message("BondLockRemoved")
def bond_lock_removed():
    return markdown(
        "✅ ",
        Bold("Bond lock removed"),
        nl(),
        "Previously locked bond is no longer retained.",
    )


@register_event_message("BondLockCompensated")
def bond_lock_compensated(amount):
    return markdown(
        "✅ ",
        Bold("Bond lock compensated"),
        nl(),
        "Compensated amount: ",
        Code(_asset_amount(amount, "ETH")),
    )


@register_event_message("BondLockPeriodChanged")
def bond_lock_period_changed(period):
    return markdown("ℹ️ ", Bold("Bond lock period changed"), nl(), "New period: ", Code(period))


@register_event_message("NodeOperatorEffectiveWeightChanged")
def node_operator_effective_weight_changed(old_weight, new_weight):
    parts: list = [
        "🚨 " if new_weight == 0 else "ℹ️ ",
        Bold("Operator effective weight changed"),
        nl(),
        "Effective weight: ",
        Code(f"{old_weight} -> {new_weight}"),
    ]
    if new_weight == 0:
        parts.extend(
            [
                nl(),
                "The Node Operator will no longer receive deposit allocation until weight is restored.",
            ]
        )
    return markdown(*parts)


def _format_sub_node_operators(sub_node_operators) -> str:
    if not sub_node_operators:
        return "none"
    return "\n".join(
        f"- {_operator_label(operator)}"
        "\n  Weighted share: "
        f"{_format_basis_points_percent(read_field(operator, 'weightedShare', 3))} "
        f"(group share: {_format_basis_points_percent(read_field(operator, 'share', 1))})"
        for operator in sub_node_operators
    )


def _format_node_operator_labels(node_operator_labels) -> str:
    if not node_operator_labels:
        return "none"
    return "\n".join(f"- {label}" for label in node_operator_labels)


def _format_basis_points_percent(value) -> str:
    basis_points = int(value)
    whole = basis_points // 100
    fraction = basis_points % 100
    if fraction == 0:
        return f"{whole}%"
    return f"{whole}.{fraction:02d}".rstrip("0") + "%"


def _operator_label(operator) -> str:
    if hasattr(operator, "label"):
        return operator.label
    if isinstance(operator, dict) and "label" in operator:
        return operator["label"]
    return f"#{read_field(operator, 'nodeOperatorId', 0)}"


def _operator_group_allocation_lines(operator, prefix: str = "") -> list:
    weighted_share_label = "Weighted share" if not prefix else f"{prefix}weighted share"
    return [
        f"{weighted_share_label}: ",
        Code(_format_basis_points_percent(read_field(operator, "weightedShare", 3))),
        f" (group share: {_format_basis_points_percent(read_field(operator, 'share', 1))})",
    ]


def _operator_group_change_allocation_lines(operator, *, previous: bool = False) -> list:
    prefix = "Previous " if previous else ""
    return [
        f"  {prefix}Share: ",
        Code(_format_basis_points_percent(read_field(operator, "share", 1))),
        nl(1),
        f"  {prefix}Effective allocation share: ",
        Code(_format_basis_points_percent(read_field(operator, "weightedShare", 3))),
    ]


def _operator_group_label(group_id, group_name: str | None = None) -> str:
    if not group_name:
        return str(group_id)
    return f"{group_id}: {group_name}"


def _operator_group_name_change_parts(
    old_group_name: str | None = None,
    new_group_name: str | None = None,
) -> list:
    if old_group_name == new_group_name or not (old_group_name or new_group_name):
        return []
    if old_group_name and new_group_name:
        return [
            "Group renamed: ",
            Code(old_group_name),
            " -> ",
            Code(new_group_name),
        ]
    if new_group_name:
        return ["Group name set: ", Code(new_group_name)]
    return ["Group name cleared: ", Code(old_group_name)]


@register_event_message("OperatorGroupCreated")
def operator_group_created(group_id, sub_node_operators, group_name=None):
    return markdown(
        "ℹ️ ",
        Bold("Operator group created"),
        nl(),
        "Group: ",
        Code(_operator_group_label(group_id, group_name=group_name)),
        nl(1),
        "Added Node Operators:",
        nl(1),
        _format_sub_node_operators(sub_node_operators),
    )


@register_event_message("OperatorGroupUpdated")
def operator_group_updated(
    group_id,
    node_operator_label=None,
    change_kind=None,
    old_operator=None,
    new_operator=None,
    group_name=None,
    old_group_name=None,
    new_group_name=None,
):
    title_prefix = "🚨 " if change_kind == "removed" else "ℹ️ "
    group_name_change_parts = _operator_group_name_change_parts(
        old_group_name=old_group_name,
        new_group_name=new_group_name,
    )
    parts: list = [
        title_prefix,
        Bold("Operator group updated"),
        nl(),
        "Group: ",
        Code(
            _operator_group_label(
                group_id,
                group_name=None if group_name_change_parts else group_name,
            )
        ),
        nl(1),
    ]
    if group_name_change_parts:
        parts.extend(group_name_change_parts)
        if node_operator_label is not None or change_kind != "renamed":
            parts.append(nl())
    else:
        parts.append(nl(1))
    match change_kind:
        case "renamed":
            pass
        case "added":
            parts.extend(
                [
                    "Changes:",
                    nl(1),
                    f"- Added {node_operator_label}",
                    nl(1),
                    *_operator_group_change_allocation_lines(new_operator),
                ]
            )
        case "changed":
            parts.extend(
                [
                    "Changes:",
                    nl(1),
                    f"- Updated {node_operator_label}",
                    nl(1),
                    "  Share: ",
                    Code(
                        f"{_format_basis_points_percent(read_field(old_operator, 'share', 1))} -> "
                        f"{_format_basis_points_percent(read_field(new_operator, 'share', 1))}"
                    ),
                    nl(1),
                    "  Effective allocation share: ",
                    Code(
                        f"{_format_basis_points_percent(read_field(old_operator, 'weightedShare', 3))} -> "
                        f"{_format_basis_points_percent(read_field(new_operator, 'weightedShare', 3))}"
                    ),
                ]
            )
        case "removed":
            parts.extend(
                [
                    "Changes:",
                    nl(1),
                    f"- Removed {node_operator_label}",
                    nl(1),
                    *_operator_group_change_allocation_lines(old_operator, previous=True),
                ]
            )
    return markdown(*parts)


@register_event_message("OperatorGroupCleared")
def operator_group_cleared(group_id, node_operator_labels, group_name=None):
    return markdown(
        "🚨 ",
        Bold("Operator group cleared"),
        nl(),
        "Group: ",
        Code(_operator_group_label(group_id, group_name=group_name)),
        nl(1),
        "Affected Node Operators:",
        nl(1),
        _format_node_operator_labels(node_operator_labels),
        nl(),
        "These Node Operators will no longer receive deposit allocation through this group.",
    )


@register_event_message("BondCurveWeightSet")
def bond_curve_weight_set(curve_id, weight):
    return markdown(
        "ℹ️ ",
        Bold("Operator type weight changed"),
        nl(),
        "Type id: ",
        Code(str(curve_id)),
        nl(1),
        "New weight: ",
        Code(str(weight)),
    )


@register_event_message("OperatorMetadataSet")
def operator_metadata_set(metadata):
    parts: list = [
        "ℹ️ ",
        Bold("Operator metadata changed"),
        nl(),
        "Name: ",
        Code(read_field(metadata, "name", 0)),
        nl(1),
        "Description: ",
        Code(read_field(metadata, "description", 1)),
    ]
    if read_field(metadata, "ownerEditsRestricted", 2) is True:
        parts.extend([nl(1), "Owner edits restricted: ", Code("true")])
    return markdown(*parts)
