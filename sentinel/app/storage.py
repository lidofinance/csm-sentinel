"""Storage helpers wrapping the persistence-backed bot and chat state."""

import json
import logging
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Set

from telegram.ext import BasePersistence, PicklePersistence

from sentinel.models import Event
from sentinel.modules.aggregation import AggregationKey, AggregationWindow

logger = logging.getLogger(__name__)


def create_persistence(storage_path: Path) -> BasePersistence:
    """Return the persistence backend used by the bot."""

    return PicklePersistence(filepath=storage_path / "persistence.pkl")


def ensure_int_set(values: Any) -> Set[int]:
    if values is None:
        return set()

    if isinstance(values, (set, frozenset)):
        items = values
        if all(isinstance(item, int) for item in items):
            return set(items) if isinstance(values, frozenset) else items
    else:
        try:
            items = set(values)
        except TypeError:  # pragma: no cover - defensive; unexpected types
            logger.warning("Ignoring malformed chat id container: %r", values)
            return set()

    result: Set[int] = set()
    for item in items:
        try:
            result.add(int(item))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            logger.warning("Skipping non-integer chat id: %r", item)
    return result


def normalise_node_operator_map(mapping: Any) -> Dict[str, Set[int]]:
    if mapping is None:
        return {}

    try:
        items = mapping.items()
    except AttributeError:  # pragma: no cover - defensive
        logger.warning("Ignoring malformed node operator mapping: %r", mapping)
        return {}

    normalised: Dict[str, Set[int]] = {}
    for key, value in items:
        str_key = str(key)
        chats = ensure_int_set(value)
        if chats:
            normalised[str_key] = chats
        else:
            # retain empty sets to preserve explicit registrations
            normalised[str_key] = set()
    return normalised


def normalise_node_operator_ids(values: Any) -> Set[str]:
    if not values:
        return set()
    try:
        return {str(value) for value in values if value is not None}
    except TypeError:  # pragma: no cover - defensive
        logger.warning("Ignoring malformed node operator list: %r", values)
        return set()


class BlockState:
    """Helper exposing the persisted latest processed block."""

    def __init__(self, bot_data: MutableMapping[str, Any]):
        self._bot_data = bot_data
        self._bot_data["block"] = int(self._bot_data.get("block", 0) or 0)

    @property
    def value(self) -> int:
        return self._bot_data["block"]

    def update(self, block: int) -> None:
        self._bot_data["block"] = int(block)

    def __int__(self) -> int:  # pragma: no cover - convenience
        return int(self.value)


class ChatIdSet:
    """Helper providing set-like operations over stored chat identifiers."""

    def __init__(self, bot_data: MutableMapping[str, Any], key: str):
        self._bot_data = bot_data
        self._key = key
        self._bot_data[self._key] = ensure_int_set(self._bot_data.get(self._key))

    def add(self, chat_id: int) -> None:
        self._values.add(int(chat_id))

    def remove(self, chat_id: int) -> None:
        self._values.discard(int(chat_id))

    def contains(self, chat_id: int) -> bool:
        return int(chat_id) in self._values

    def all(self) -> Set[int]:
        return set(self._values)

    def migrate_chat_id(self, old_chat_id: int, new_chat_id: int) -> bool:
        """Replace an existing chat id with a new one.

        Returns True when the set was updated.
        """

        old_id = int(old_chat_id)
        new_id = int(new_chat_id)
        if old_id == new_id:
            return False
        if old_id not in self._values:
            return False
        self._values.discard(old_id)
        self._values.add(new_id)
        return True

    @property
    def _values(self) -> Set[int]:
        return self._bot_data[self._key]


class NodeOperatorChats:
    """Helper for mapping node operator identifiers to subscribed chats."""

    def __init__(self, bot_data: MutableMapping[str, Any], key: str = "no_ids_to_chats"):
        self._bot_data = bot_data
        self._key = key
        self._bot_data[self._key] = normalise_node_operator_map(self._bot_data.get(self._key))

    def subscribe(self, node_operator_id: str, chat_id: int) -> None:
        key = self._normalise_node_operator_id(node_operator_id)
        chats = self._mapping.setdefault(key, set())
        chats.add(int(chat_id))

    def unsubscribe(self, node_operator_id: str, chat_id: int) -> None:
        key = self._normalise_node_operator_id(node_operator_id)
        chats = self._mapping.get(key)
        if chats is None:
            return
        chats.discard(int(chat_id))
        if not chats:
            # keep empty sets to avoid accidental re-creation churn
            self._mapping[key] = set()

    def chats_for(self, node_operator_id: str) -> Set[int]:
        key = self._normalise_node_operator_id(node_operator_id)
        return set(self._mapping.get(key, set()))

    def ids(self) -> Set[str]:
        return set(self._mapping.keys())

    def resolve_targets(
        self,
        node_operator_ids: Iterable[str],
        actual_chat_ids: Iterable[int],
    ) -> Set[int]:
        desired = {self._normalise_node_operator_id(no_id) for no_id in node_operator_ids}
        actual = set(actual_chat_ids)
        targets: Set[int] = set()
        for no_id in desired:
            targets.update(self._mapping.get(no_id, set()))
        return targets.intersection(actual)

    def subscription_counts(
        self,
        actual_chat_ids: Iterable[int],
        user_ids: Iterable[int],
        group_ids: Iterable[int],
        channel_ids: Iterable[int],
    ) -> Dict[str, Dict[str, int]]:
        actual = set(actual_chat_ids)
        users = set(user_ids)
        groups = set(group_ids)
        channels = set(channel_ids)

        results: Dict[str, Dict[str, int]] = {}
        for no_id, chats in self._mapping.items():
            active = chats.intersection(actual)
            if not active:
                continue
            results[no_id] = {
                "total": len(active),
                "users": len(active.intersection(users)),
                "groups": len(active.intersection(groups)),
                "channels": len(active.intersection(channels)),
            }
        return results

    def migrate_chat_id(self, old_chat_id: int, new_chat_id: int) -> int:
        """Replace an existing chat id with a new one across all node operators.

        Returns the number of node operators whose chat set was updated.
        """

        old_id = int(old_chat_id)
        new_id = int(new_chat_id)
        if old_id == new_id:
            return 0

        updated = 0
        for chats in self._mapping.values():
            if old_id in chats:
                chats.discard(old_id)
                chats.add(new_id)
                updated += 1
        return updated

    @property
    def _mapping(self) -> Dict[str, Set[int]]:
        return self._bot_data[self._key]

    @staticmethod
    def _normalise_node_operator_id(node_operator_id: str) -> str:
        return str(node_operator_id)


class AggregationWindowStore:
    """Helper exposing persisted aggregation windows."""

    KEY = "aggregation_windows"

    def __init__(self, bot_data: MutableMapping[str, Any]):
        self._bot_data = bot_data
        self._bot_data[self.KEY] = self._normalise_windows(self._bot_data.get(self.KEY))

    def upsert_pending(self, window: AggregationWindow) -> None:
        record = AggregationWindowRecord(window=window)
        self._records[record.key] = record.to_dict()

    def discard(self, window: AggregationWindow) -> None:
        self._records.pop(AggregationWindowRecord.key_for(window), None)

    def pending(self) -> list[AggregationWindow]:
        return [record.window for record in self._records_from(self._records)]

    def pending_for(
        self,
        group: str,
        aggregation_key: AggregationKey,
        block: int,
    ) -> AggregationWindow | None:
        for record in self._records_from(self._records):
            if (
                record.window.group == group
                and record.window.aggregation_key == aggregation_key
                and record.window.contains(block)
            ):
                return record.window
        return None

    @classmethod
    def pop_group_events(
        cls,
        bot_data: MutableMapping[str, Any],
        group: str,
    ) -> list[Event]:
        raw_windows = bot_data.get(cls.KEY)
        if not isinstance(raw_windows, dict):
            return []

        events: list[Event] = []
        for key, record in list(raw_windows.items()):
            if not isinstance(record, dict) or record.get("group") != group:
                continue
            raw_windows.pop(key)
            events.extend(DigestStore.parse_events(record.get("events")))
        return events

    @property
    def _records(self) -> dict[str, dict[str, Any]]:
        return self._bot_data[self.KEY]

    @classmethod
    def _records_from(cls, raw_records: dict[str, Any]) -> list["AggregationWindowRecord"]:
        parsed_records: list[AggregationWindowRecord] = []
        for record in raw_records.values():
            try:
                parsed_record = AggregationWindowRecord.from_dict(record)
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed aggregation window record: %r", record)
                continue
            parsed_records.append(parsed_record)
        return parsed_records

    @classmethod
    def _normalise_windows(cls, value: Any) -> dict[str, dict[str, Any]]:
        if not value:
            return {}
        if not isinstance(value, dict):
            logger.warning("Ignoring malformed aggregation window state: %r", value)
            return {}
        return {record.key: record.to_dict() for record in cls._records_from(value)}


@dataclass(frozen=True, slots=True)
class AggregationWindowRecord:
    window: AggregationWindow

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "AggregationWindowRecord":
        raw_key = record["aggregation_key"]
        return cls(
            window=AggregationWindow(
                group=str(record["group"]),
                aggregation_key=AggregationKey(
                    kind=str(raw_key["kind"]),
                    value=str(raw_key["value"]),
                ),
                start_block=int(record["start_block"]),
                end_block=int(record["end_block"]),
                event_names=frozenset(str(name) for name in record["event_names"]),
                events=tuple(Event.from_dict(event) for event in record["events"]),
            )
        )

    @property
    def key(self) -> str:
        return self.key_for(self.window)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.window.group,
            "aggregation_key": {
                "kind": self.window.aggregation_key.kind,
                "value": self.window.aggregation_key.value,
            },
            "start_block": int(self.window.start_block),
            "end_block": int(self.window.end_block),
            "event_names": sorted(self.window.event_names),
            "events": [event.to_dict() for event in self.window.events],
        }

    @staticmethod
    def key_for(window: AggregationWindow) -> str:
        return json.dumps(
            [
                window.group,
                window.aggregation_key.kind,
                window.aggregation_key.value,
                window.start_block,
                window.end_block,
            ],
            separators=(",", ":"),
        )


class DigestStore:
    """Persist events until a digest is sent."""

    KEY = "digests"

    def __init__(self, bot_data: MutableMapping[str, Any]):
        self._bot_data = bot_data
        self._bot_data[self.KEY] = self._normalise(bot_data.get(self.KEY))

    def events(self, name: str) -> tuple[Event, ...]:
        return tuple(Event.from_dict(record) for record in self._records.get(name, []))

    def append(self, name: str, event: Event) -> None:
        self.replace(name, (*self.events(name), event))

    def extend(self, name: str, events: Iterable[Event]) -> None:
        self.replace(name, (*self.events(name), *events))

    def discard(self, name: str, events: tuple[Event, ...]) -> None:
        discarded = {event.log_identity for event in events}
        self.replace(
            name,
            (event for event in self.events(name) if event.log_identity not in discarded),
        )

    def replace(self, name: str, events: Iterable[Event]) -> None:
        events_by_identity = {event.log_identity: event for event in events}
        ordered = sorted(
            events_by_identity.values(),
            key=lambda event: (event.block, event.transaction_index, event.log_index),
        )
        self._records[name] = [event.to_dict() for event in ordered]

    @property
    def _records(self) -> dict[str, list[dict[str, Any]]]:
        return self._bot_data[self.KEY]

    @classmethod
    def _normalise(cls, value: Any) -> dict[str, list[dict[str, Any]]]:
        if not value:
            return {}
        if not isinstance(value, dict):
            logger.warning("Ignoring malformed digest state: %r", value)
            return {}
        return {
            str(name): [event.to_dict() for event in cls.parse_events(records)]
            for name, records in value.items()
        }

    @classmethod
    def parse_events(cls, value: Any) -> list[Event]:
        if not value:
            return []
        if not isinstance(value, list):
            logger.warning("Ignoring malformed digest event state: %r", value)
            return []
        events: list[Event] = []
        for record in value:
            try:
                events.append(Event.from_dict(record))
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed digest event: %r", record)
        return events


class ScheduledJobStore:
    """Persistence-backed completion markers for calendar-scheduled jobs."""

    KEY = "scheduled_jobs"

    def __init__(self, bot_data: MutableMapping[str, Any]):
        self._bot_data = bot_data
        self._bot_data[self.KEY] = self._normalise(bot_data.get(self.KEY))

    def completed_for(self, job_name: str) -> datetime | None:
        record = self._records.get(job_name)
        if record is None:
            return None
        return datetime.fromisoformat(record["completed_for"]).astimezone(UTC)

    def mark_completed(self, job_name: str, scheduled_for: datetime) -> None:
        if scheduled_for.tzinfo is None:
            raise ValueError("scheduled job completion time must be timezone-aware")
        self._records[job_name] = {
            "completed_for": scheduled_for.astimezone(UTC).isoformat(),
        }

    @property
    def _records(self) -> dict[str, dict[str, str]]:
        return self._bot_data[self.KEY]

    @classmethod
    def _normalise(cls, value: Any) -> dict[str, dict[str, str]]:
        if not value:
            return {}
        if not isinstance(value, dict):
            logger.warning("Ignoring malformed scheduled job state: %r", value)
            return {}

        records: dict[str, dict[str, str]] = {}
        for job_name, raw_record in value.items():
            try:
                raw_completed_for = raw_record["completed_for"]
                completed_for = datetime.fromisoformat(raw_completed_for)
                if completed_for.tzinfo is None:
                    raise ValueError("completed_for must be timezone-aware")
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed scheduled job record: %r", raw_record)
                continue
            records[str(job_name)] = {
                "completed_for": completed_for.astimezone(UTC).isoformat(),
            }
        return records


class BotStorage:
    """Utility wrapper around the application-wide bot data."""

    def __init__(self, bot_data: MutableMapping[str, Any]):
        self._bot_data = bot_data
        self._block = BlockState(bot_data)
        self._users = ChatIdSet(bot_data, "user_ids")
        self._groups = ChatIdSet(bot_data, "group_ids")
        self._channels = ChatIdSet(bot_data, "channel_ids")
        self._node_operator_chats = NodeOperatorChats(bot_data)
        self._digests = DigestStore(bot_data)
        self._aggregation_windows = AggregationWindowStore(bot_data)
        self._scheduled_jobs = ScheduledJobStore(bot_data)

    @property
    def block(self) -> BlockState:
        return self._block

    @property
    def users(self) -> ChatIdSet:
        return self._users

    @property
    def groups(self) -> ChatIdSet:
        return self._groups

    @property
    def channels(self) -> ChatIdSet:
        return self._channels

    @property
    def node_operator_chats(self) -> NodeOperatorChats:
        return self._node_operator_chats

    @property
    def digests(self) -> DigestStore:
        return self._digests

    @property
    def aggregation_windows(self) -> AggregationWindowStore:
        return self._aggregation_windows

    @property
    def scheduled_jobs(self) -> ScheduledJobStore:
        return self._scheduled_jobs

    def actual_chat_ids(self) -> Set[int]:
        return self.users.all().union(self.groups.all(), self.channels.all())

    def resolve_target_chats(self, node_operator_ids: Iterable[str]) -> Set[int]:
        return self.node_operator_chats.resolve_targets(node_operator_ids, self.actual_chat_ids())

    def subscription_counts(self) -> Dict[str, Dict[str, int]]:
        return self.node_operator_chats.subscription_counts(
            self.actual_chat_ids(),
            self.users.all(),
            self.groups.all(),
            self.channels.all(),
        )

    def migrate_chat_id(self, old_chat_id: int, new_chat_id: int) -> None:
        """Update stored indexes for a migrated chat id."""

        self.users.migrate_chat_id(old_chat_id, new_chat_id)
        self.groups.migrate_chat_id(old_chat_id, new_chat_id)
        self.channels.migrate_chat_id(old_chat_id, new_chat_id)
        self.node_operator_chats.migrate_chat_id(old_chat_id, new_chat_id)


class NodeOperatorSubscriptions:
    """Helper around per-chat node operator subscriptions."""

    def __init__(self, chat_data: MutableMapping[str, Any], key: str = "node_operators"):
        self._chat_data = chat_data
        self._key = key
        self._chat_data[self._key] = normalise_node_operator_ids(chat_data.get(self._key))

    def ids(self) -> Set[str]:
        return set(self._values)

    def follow(self, node_operator_id: str) -> None:
        self._values.add(str(node_operator_id))

    def unfollow(self, node_operator_id: str) -> bool:
        key = str(node_operator_id)
        if key in self._values:
            self._values.remove(key)
            return True
        return False

    @property
    def _values(self) -> Set[str]:
        return self._chat_data[self._key]


class ChatStorage:
    """Utility wrapper around per-chat data."""

    def __init__(self, chat_data: MutableMapping[str, Any]):
        self._chat_data = chat_data
        self._node_operators = NodeOperatorSubscriptions(chat_data)

    @property
    def node_operators(self) -> NodeOperatorSubscriptions:
        return self._node_operators
