import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from sentinel.models import Event, EventHandler, EventNotification

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sentinel.app.storage import DigestStore


class DigestGroups:
    DEPOSITED_SIGNING_KEYS = "deposited_signing_keys"


class Digest:
    def __init__(
        self,
        name: str,
        event_name: str,
        store: Callable[[], "DigestStore"],
        emit_notification: Callable[[EventNotification], Awaitable[None]],
    ) -> None:
        self.name = name
        self._event_name = event_name
        self._store = store
        self._emit_notification = emit_notification
        self._lock = asyncio.Lock()

    @property
    def pending_count(self) -> int:
        return len(self._store().events(self.name))

    async def handle_event(self, event: Event) -> bool:
        if event.event != self._event_name:
            return False
        async with self._lock:
            self._store().append(self.name, event)
        return True

    async def flush_through(self, block: int) -> int:
        async with self._lock:
            ready = tuple(
                event for event in self._store().events(self.name) if event.block <= block
            )
            if not ready:
                return 0

            await self._emit_notification(EventNotification(ready))
            self._store().discard(self.name, ready)
            logger.info(
                "Digest flushed",
                extra={
                    "through_block": block,
                    "source_event_count": len(ready),
                },
            )
            return 1


def build_digests(
    event_handlers: Mapping[str, EventHandler],
    store: Callable[[], "DigestStore"],
    emit_notification: Callable[[EventNotification], Awaitable[None]],
) -> dict[str, Digest]:
    event_name_by_digest: dict[str, str] = {}
    for event_handler in event_handlers.values():
        if event_handler.digest_name is None:
            continue
        previous_event = event_name_by_digest.get(event_handler.digest_name)
        if previous_event is not None:
            raise RuntimeError(
                f"Digest {event_handler.digest_name!r} is assigned to multiple events: "
                f"{previous_event!r}, {event_handler.event!r}"
            )
        event_name_by_digest[event_handler.digest_name] = event_handler.event

    return {
        name: Digest(
            name,
            event_name,
            store,
            emit_notification,
        )
        for name, event_name in event_name_by_digest.items()
    }
