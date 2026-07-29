import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from sentinel.models import Event, EventNotification
from sentinel.metrics.aggregation import (
    AggregationMetricsMiddleware,
)
from sentinel.metrics.registry import DEFAULT_METRICS
from sentinel.modules.aggregation import AggregationWindow, EventAggregator

logger = logging.getLogger(__name__)
DEFAULT_AGGREGATION_METRICS = AggregationMetricsMiddleware(DEFAULT_METRICS.aggregation)


class BlockProgressStore(Protocol):
    value: int

    def update(self, block: int) -> None: ...


class AggregationWindowStore(Protocol):
    def pending(self) -> list[AggregationWindow]: ...

    def upsert_pending(self, window: AggregationWindow) -> None: ...

    def discard(self, window: AggregationWindow) -> None: ...

    def mark_aggregated(self, window: AggregationWindow) -> None: ...

    def prune(self, current_block: int) -> None: ...

    def contains_active(self, group: str, block: int) -> bool: ...


class ProcessingState(Protocol):
    @property
    def block(self) -> BlockProgressStore: ...

    @property
    def aggregation_windows(self) -> AggregationWindowStore: ...


class ProcessingStateProvider(Protocol):
    @property
    def state(self) -> ProcessingState: ...


class BlockNumberReader(Protocol):
    async def get_block_number(self) -> int: ...


class EventHistory(Protocol):
    async def fetch_events(self, start_block: int, end_block: int) -> list[Event]: ...


class NotificationSink(Protocol):
    async def emit(self, notification: EventNotification) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparedNotifications:
    notifications: list[EventNotification]
    aggregated_window: AggregationWindow | None = None


class AggregationCoordinator:
    """Coordinate aggregation windows without knowing the transport or sink details."""

    def __init__(
        self,
        *,
        storage: ProcessingStateProvider,
        event_history: EventHistory,
        block_reader: BlockNumberReader,
        notification_sink: NotificationSink,
        aggregators: tuple[EventAggregator, ...],
        metrics: AggregationMetricsMiddleware = DEFAULT_AGGREGATION_METRICS,
        poll_interval_seconds: float = 12.0,
    ) -> None:
        self._storage = storage
        self._event_history = event_history
        self._block_reader = block_reader
        self._notification_sink = notification_sink
        self._poll_interval_seconds = poll_interval_seconds
        self._metrics = metrics
        self._aggregating_windows: set[AggregationWindow] = set()
        self._pending_tasks: dict[AggregationWindow, asyncio.Task] = {}
        self._aggregators_by_group = {
            aggregator.group.name: aggregator for aggregator in aggregators
        }
        self._aggregators_by_event = {
            event_name: aggregator
            for aggregator in aggregators
            for event_name in aggregator.event_names
        }

    @property
    def _aggregation_windows(self):
        return self._storage.state.aggregation_windows

    @property
    def pending_window_count(self) -> int:
        return len(self._aggregation_windows.pending())

    async def handle_event(self, event: Event) -> None:
        prepared = await self._prepare(event)
        await self._emit_prepared(prepared)

    async def _prepare(self, event: Event) -> PreparedNotifications:
        aggregator = self._aggregators_by_event.get(event.event)
        if aggregator is None:
            return PreparedNotifications([EventNotification.from_event(event)])

        self._aggregation_windows.prune(event.block)
        if self._has_active_window(aggregator.group.name, event.block):
            return PreparedNotifications([])

        window = aggregator.window_for(event.block)
        self._aggregation_windows.upsert_pending(window)
        if window.end_block > event.block and not await self._window_is_ready(window):
            self._schedule_window(window, aggregator)
            return PreparedNotifications([])

        return await self._metrics.run(
            window.group,
            lambda: self._aggregate_window(window, aggregator, fallback_event=event),
        ) or PreparedNotifications([])

    def resume_pending(self) -> None:
        for window in self._aggregation_windows.pending():
            aggregator = self._aggregator_for_window(window)
            if aggregator is None:
                logger.warning(
                    "Cannot resume aggregation window without registered aggregator",
                    extra={"group": window.group, "event_names": sorted(window.event_names)},
                )
                continue
            self._schedule_window(window, aggregator)

    async def close(self) -> None:
        tasks = tuple(self._pending_tasks.values())
        for task in tasks:
            task.cancel()
        self._pending_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _has_active_window(self, group: str, block: int) -> bool:
        return (
            self._aggregation_windows.contains_active(group, block)
            or self._runtime_window_contains(self._aggregating_windows, group, block)
            or self._runtime_window_contains(set(self._pending_tasks), group, block)
        )

    @staticmethod
    def _runtime_window_contains(windows: set[AggregationWindow], group: str, block: int) -> bool:
        return any(window.group == group and window.contains(block) for window in windows)

    async def _window_is_ready(self, window: AggregationWindow) -> bool:
        return await self._block_reader.get_block_number() >= window.end_block

    def _schedule_window(self, window: AggregationWindow, aggregator: EventAggregator) -> None:
        if window in self._pending_tasks:
            return
        self._pending_tasks[window] = asyncio.create_task(
            self._run_pending_window(window, aggregator)
        )

    async def _run_pending_window(
        self, window: AggregationWindow, aggregator: EventAggregator
    ) -> None:
        try:
            while True:
                while not await self._window_is_ready(window):
                    await asyncio.sleep(self._poll_interval_seconds)

                prepared = await self._metrics.run(
                    window.group,
                    lambda: self._aggregate_window(window, aggregator, fallback_event=None),
                )
                if prepared is None:
                    await asyncio.sleep(self._poll_interval_seconds)
                    continue

                await self._emit_prepared(prepared)
                return
        except asyncio.CancelledError:
            raise
        finally:
            self._pending_tasks.pop(window, None)

    async def _aggregate_window(
        self,
        window: AggregationWindow,
        aggregator: EventAggregator,
        *,
        fallback_event: Event | None,
    ) -> PreparedNotifications | None:
        self._aggregating_windows.add(window)
        try:
            try:
                block_events = await self._event_history.fetch_events(
                    window.start_block, window.end_block
                )
            except Exception as exc:
                logger.warning(
                    "Failed to aggregate events for blocks %s-%s: %s",
                    window.start_block,
                    window.end_block,
                    exc,
                    extra={"group": window.group},
                    exc_info=True,
                )
                if fallback_event is None:
                    return None
                self._aggregation_windows.discard(window)
                return PreparedNotifications([EventNotification.from_event(fallback_event)])

            if not block_events:
                logger.warning(
                    "Aggregation window has no matching events",
                    extra={
                        "group": window.group,
                        "start_block": window.start_block,
                        "end_block": window.end_block,
                    },
                )
                if fallback_event is None:
                    return PreparedNotifications([], aggregated_window=window)
                self._aggregation_windows.discard(window)
                return PreparedNotifications([EventNotification.from_event(fallback_event)])

            return PreparedNotifications(
                aggregator.aggregate(block_events),
                aggregated_window=window,
            )
        finally:
            self._aggregating_windows.discard(window)

    async def _emit_prepared(self, prepared: PreparedNotifications) -> None:
        for notification in prepared.notifications:
            await self._notification_sink.emit(notification)
        if prepared.aggregated_window is not None:
            self._aggregation_windows.mark_aggregated(prepared.aggregated_window)
            self._aggregation_windows.prune(prepared.aggregated_window.end_block)

    def _aggregator_for_window(self, window: AggregationWindow) -> EventAggregator | None:
        aggregator = self._aggregators_by_group.get(window.group)
        if aggregator is not None:
            return aggregator
        for event_name in window.event_names:
            aggregator = self._aggregators_by_event.get(event_name)
            if aggregator is not None:
                return aggregator
        return None
