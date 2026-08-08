from sentinel.models import EventHandler
from sentinel.modules.event_engine import MessageTemplate


class RegisterEventHandler:
    def __init__(
        self,
        registry: dict[str, EventHandler],
        event_name: str,
        aggregation_group=None,
        digest_name: str | None = None,
    ):
        self.registry = registry
        self.event_name = event_name
        self.aggregation_group = aggregation_group
        self.digest_name = digest_name

    def __call__(self, func):
        self.registry[self.event_name] = EventHandler(
            self.event_name,
            func,
            aggregation_group=self.aggregation_group,
            digest_name=self.digest_name,
        )
        return func


class RegisterEventMessage:
    def __init__(self, registry: dict[str, MessageTemplate], event_name: str):
        self.registry = registry
        self.event_name = event_name

    def __call__(self, func: MessageTemplate) -> MessageTemplate:
        self.registry[self.event_name] = func
        return func
