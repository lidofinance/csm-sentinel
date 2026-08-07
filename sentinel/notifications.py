from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar

OperatorId = TypeVar("OperatorId", int, str)


@dataclass(frozen=True, slots=True)
class BroadcastDelivery:
    message: str
    operator_ids: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class OperatorMessagesDelivery:
    messages: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PerChatDelivery:
    operator_ids: frozenset[str]
    render: Callable[[frozenset[str]], tuple[str, ...]]


NotificationDelivery = BroadcastDelivery | OperatorMessagesDelivery | PerChatDelivery


@dataclass(frozen=True, slots=True)
class NotificationPlan:
    """One explicit delivery strategy for an event notification."""

    delivery: NotificationDelivery

    @classmethod
    def broadcast(cls, message: str) -> "NotificationPlan":
        return cls(BroadcastDelivery(message=message))

    @classmethod
    def broadcast_to_operators(
        cls,
        message: str,
        operator_ids: Iterable[int | str],
    ) -> "NotificationPlan":
        return cls(
            BroadcastDelivery(
                message=message,
                operator_ids=_normalise_operator_ids(operator_ids),
            )
        )

    @classmethod
    def per_operator(
        cls,
        messages: Mapping[OperatorId, str],
    ) -> "NotificationPlan":
        normalised_messages = {str(no_id): message for no_id, message in messages.items()}
        if not normalised_messages:
            raise ValueError("per-operator delivery requires at least one message")
        return cls(OperatorMessagesDelivery(messages=normalised_messages))

    @classmethod
    def per_chat(
        cls,
        node_operator_ids: Iterable[int | str],
        render: Callable[[frozenset[str]], tuple[str, ...]],
    ) -> "NotificationPlan":
        operator_ids = _normalise_operator_ids(node_operator_ids)
        if not operator_ids:
            raise ValueError("per-chat delivery requires at least one operator")
        return cls(PerChatDelivery(operator_ids=operator_ids, render=render))


def _normalise_operator_ids(values: Iterable[int | str]) -> frozenset[str]:
    return frozenset(str(value) for value in values)
