from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, replace

from sentinel.models import Event, EventNotification
from sentinel.modules.formatting import read_field

OPERATOR_GROUP_CREATED = "OperatorGroupCreated"
OPERATOR_GROUP_UPDATED = "OperatorGroupUpdated"
OPERATOR_GROUP_CLEARED = "OperatorGroupCleared"
NODE_OPERATOR_EFFECTIVE_WEIGHT_CHANGED = "NodeOperatorEffectiveWeightChanged"
BOND_CURVE_WEIGHT_SET = "BondCurveWeightSet"


@dataclass(frozen=True, slots=True)
class AggregationGroup:
    name: str
    window_blocks: int = 1

    def __post_init__(self) -> None:
        if self.window_blocks < 1:
            raise ValueError("window_blocks must be at least 1")


class AggregationGroups:
    TOTAL_SIGNING_KEY_COUNTS = AggregationGroup(
        name="total_signing_key_counts",
        window_blocks=1,
    )
    VALIDATOR_EXIT_REQUESTS = AggregationGroup(
        name="validator_exit_requests",
        window_blocks=1,
    )
    VALIDATOR_WITHDRAWALS = AggregationGroup(
        name="validator_withdrawals",
        window_blocks=5,
    )
    OPERATOR_GROUP_CHANGES = AggregationGroup(
        name="operator_group_changes",
        window_blocks=1,
    )


@dataclass(frozen=True, slots=True)
class AggregationWindow:
    group: str
    aggregation_key: "AggregationKey"
    start_block: int
    end_block: int
    event_names: frozenset[str]
    events: tuple[Event, ...] = ()

    def contains(self, block: int) -> bool:
        return self.start_block <= block <= self.end_block

    def with_event(self, event: Event) -> "AggregationWindow":
        if any(stored_event.log_identity == event.log_identity for stored_event in self.events):
            return self
        return replace(self, events=(*self.events, event))


@dataclass(frozen=True, slots=True)
class AggregationKey:
    kind: str
    value: str

    @classmethod
    def global_key(cls) -> "AggregationKey":
        return cls(kind="global", value="all")


class EventAggregator(ABC):
    group: AggregationGroup
    event_names: frozenset[str]

    @abstractmethod
    def aggregation_key(self, event: Event) -> AggregationKey: ...

    @abstractmethod
    def window_for(self, event: Event) -> AggregationWindow: ...

    @abstractmethod
    def aggregate(self, events: Iterable[Event]) -> list[EventNotification]: ...


@dataclass(frozen=True, slots=True)
class NodeOperatorEventAggregator(EventAggregator):
    group: AggregationGroup
    event_names: frozenset[str]

    def aggregation_key(self, event: Event) -> AggregationKey:
        return AggregationKey(
            kind="node_operator",
            value=str(event.args["nodeOperatorId"]),
        )

    def window_for(self, event: Event) -> AggregationWindow:
        return AggregationWindow(
            group=self.group.name,
            aggregation_key=self.aggregation_key(event),
            start_block=event.block,
            end_block=event.block + self.group.window_blocks - 1,
            event_names=self.event_names,
        )

    def aggregate(self, events: Iterable[Event]) -> list[EventNotification]:
        aggregatable_events = sorted(
            (event for event in events if event.event in self.event_names),
            key=lambda event: (event.block, event.transaction_index, event.log_index),
        )
        events_by_key: dict[tuple[str, int], list[Event]] = {}

        for event in aggregatable_events:
            node_operator_id = int(event.args["nodeOperatorId"])
            events_by_key.setdefault((event.event, node_operator_id), []).append(event)

        return [
            EventNotification(source_events=tuple(operator_events))
            for _, operator_events in sorted(events_by_key.items())
        ]


@dataclass(frozen=True, slots=True)
class OperatorGroupChangeAggregator(EventAggregator):
    group: AggregationGroup = AggregationGroups.OPERATOR_GROUP_CHANGES
    event_names: frozenset[str] = frozenset(
        {
            OPERATOR_GROUP_CREATED,
            OPERATOR_GROUP_UPDATED,
            OPERATOR_GROUP_CLEARED,
            NODE_OPERATOR_EFFECTIVE_WEIGHT_CHANGED,
            BOND_CURVE_WEIGHT_SET,
        }
    )

    def aggregation_key(self, event: Event) -> AggregationKey:
        return AggregationKey.global_key()

    def window_for(self, event: Event) -> AggregationWindow:
        return AggregationWindow(
            group=self.group.name,
            aggregation_key=self.aggregation_key(event),
            start_block=event.block,
            end_block=event.block + self.group.window_blocks - 1,
            event_names=self.event_names,
        )

    def aggregate(self, events: Iterable[Event]) -> list[EventNotification]:
        relevant_events = sorted(
            (event for event in events if event.event in self.event_names),
            key=lambda event: (event.block, event.transaction_index, event.log_index),
        )
        trigger_events = [
            event for event in relevant_events if event.event in _OPERATOR_GROUP_TRIGGER_EVENTS
        ]
        if not trigger_events:
            return [EventNotification.from_event(event) for event in relevant_events]

        notifications: list[EventNotification] = []
        for group_events in _events_by_group_id(trigger_events).values():
            notification = self._notification_for_group(group_events)
            if notification is not None:
                notifications.append(notification)

        consumed_node_operator_ids = _final_group_node_operator_ids(trigger_events)
        notifications.extend(
            EventNotification.from_event(event)
            for event in relevant_events
            if event.event not in _OPERATOR_GROUP_TRIGGER_EVENTS
            and not _is_consumed_supporting_event(event, consumed_node_operator_ids)
        )
        return sorted(
            notifications,
            key=lambda notification: (
                notification.block,
                notification.primary_event.transaction_index,
                notification.primary_event.log_index,
            ),
        )

    def _notification_for_group(self, group_events: list[Event]) -> EventNotification | None:
        last_event = group_events[-1]
        if last_event.event == OPERATOR_GROUP_CLEARED:
            return EventNotification(source_events=tuple(group_events))

        final_group_event = _last_group_info_event(group_events)
        if final_group_event is None:
            return None

        if _contains_event(group_events, OPERATOR_GROUP_CLEARED):
            event_name = OPERATOR_GROUP_UPDATED
        elif _contains_event(group_events, OPERATOR_GROUP_CREATED):
            event_name = OPERATOR_GROUP_CREATED
        else:
            event_name = OPERATOR_GROUP_UPDATED

        if final_group_event.event == event_name:
            return EventNotification(source_events=tuple(group_events))
        event = replace(final_group_event, event=event_name)
        return EventNotification(source_events=(*group_events, event))


def node_operator_aggregators_from_event_handlers(
    event_handlers,
) -> tuple[EventAggregator, ...]:
    event_names_by_group: dict[AggregationGroup, set[str]] = {}
    for event_handler in event_handlers.values():
        aggregation_group = event_handler.aggregation_group
        if aggregation_group is None:
            continue
        event_names_by_group.setdefault(aggregation_group, set()).add(event_handler.event)

    return tuple(
        NodeOperatorEventAggregator(group=aggregation_group, event_names=frozenset(event_names))
        for aggregation_group, event_names in event_names_by_group.items()
    )


_OPERATOR_GROUP_TRIGGER_EVENTS = frozenset(
    {
        OPERATOR_GROUP_CREATED,
        OPERATOR_GROUP_UPDATED,
        OPERATOR_GROUP_CLEARED,
    }
)


def _events_by_group_id(events: Iterable[Event]) -> dict[int, list[Event]]:
    events_by_group_id: dict[int, list[Event]] = {}
    for event in events:
        events_by_group_id.setdefault(int(event.args["groupId"]), []).append(event)
    return events_by_group_id


def _last_group_info_event(events: Iterable[Event]) -> Event | None:
    for event in reversed(tuple(events)):
        if event.event in {OPERATOR_GROUP_CREATED, OPERATOR_GROUP_UPDATED}:
            return event
    return None


def _final_group_node_operator_ids(events: Iterable[Event]) -> set[int]:
    node_operator_ids: set[int] = set()
    for event in events:
        group_info = event.args.get("groupInfo")
        if group_info is None:
            continue
        for operator in read_field(group_info, "subNodeOperators", 1):
            node_operator_ids.add(int(read_field(operator, "nodeOperatorId", 0)))
    return node_operator_ids


def _is_consumed_supporting_event(event: Event, node_operator_ids: set[int]) -> bool:
    node_operator_id = event.args.get("nodeOperatorId")
    return (
        event.event == NODE_OPERATOR_EFFECTIVE_WEIGHT_CHANGED
        and node_operator_id is not None
        and int(node_operator_id) in node_operator_ids
    )


def _contains_event(events: Iterable[Event], event_name: str) -> bool:
    return any(event.event == event_name for event in events)
