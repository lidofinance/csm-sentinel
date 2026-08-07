from dataclasses import dataclass
from typing import Any

from eth_utils import event_abi_to_log_topic, get_all_event_abis
from web3 import AsyncWeb3
from web3._utils.events import get_event_data
from web3.types import EventData
from web3.types import FilterParams

from sentinel.models import Event
from sentinel.modules.base import BaseModuleAdapter, EventSource


@dataclass(frozen=True, slots=True)
class EventBindingSet:
    event_sources: tuple[EventSource, ...]
    abi_by_topics: dict


def build_event_bindings(module_adapter: BaseModuleAdapter) -> EventBindingSet:
    event_names = module_adapter.notifiable_events() | module_adapter.side_effect_events()
    return EventBindingSet(
        event_sources=module_adapter.event_sources(),
        abi_by_topics=topics_to_follow(event_names, *module_adapter.topic_abis()),
    )


def topics_to_follow(event_names: set[str], *abis) -> dict:
    topics = {}
    for event in [event for abi in abis for event in get_all_event_abis(abi)]:
        if event["name"] not in event_names:
            continue

        topic = event_abi_to_log_topic(event)
        existing = topics.get(topic)
        if existing is not None:
            if _event_decoder_shape(existing) != _event_decoder_shape(event):
                raise RuntimeError(
                    f"Event topic collision for {event['name']} with incompatible ABI inputs"
                )
            continue

        topics[topic] = event
    return topics


def decode_event(w3: AsyncWeb3, event_abi: dict, log: dict[str, Any]) -> Event:
    event_data: EventData = get_event_data(w3.codec, event_abi, log)
    return Event(
        event=event_data["event"],
        args=event_data["args"],
        block=event_data["blockNumber"],
        tx=event_data["transactionHash"],
        address=event_data["address"],
        log_index=event_data["logIndex"],
        transaction_index=event_data["transactionIndex"],
    )


def topic_filter_for_events(abi_by_topics: dict, event_names: set[str]) -> list[Any]:
    topics = [
        topic for topic, event_abi in abi_by_topics.items() if event_abi["name"] in event_names
    ]
    if not topics:
        raise RuntimeError(f"No ABI topics configured for events: {sorted(event_names)}")
    if len(topics) == 1:
        return topics
    return [topics]


def log_filter_for_source(source: EventSource, abi_by_topics: dict) -> FilterParams | None:
    if source.event_names is not None and not source.event_names:
        return None

    filter_params = FilterParams(address=source.address)
    if source.event_names is not None:
        filter_params["topics"] = topic_filter_for_events(abi_by_topics, set(source.event_names))
    return filter_params


def _event_decoder_shape(event_abi: Any) -> tuple[str, tuple[tuple[str, bool], ...]]:
    return (
        event_abi["name"],
        tuple((item["type"], bool(item.get("indexed"))) for item in event_abi.get("inputs", [])),
    )
