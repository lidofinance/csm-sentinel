import logging
from dataclasses import dataclass
from typing import Protocol

from sentinel.metrics.aggregation import AggregationMetricsMiddleware
from sentinel.metrics.registry import DEFAULT_METRICS
from sentinel.models import Event, EventNotification
from sentinel.modules.aggregation import (
    AggregationKey,
    AggregationWindow,
    EventAggregator,
)

DEFAULT_AGGREGATION_METRICS = AggregationMetricsMiddleware(DEFAULT_METRICS.aggregation)
logger = logging.getLogger(__name__)


class BlockProgressStore(Protocol):
    value: int

    def update(self, block: int) -> None: ...


class AggregationWindowStore(Protocol):
    def pending(self) -> list[AggregationWindow]: ...

    def pending_for(
        self,
        group: str,
        aggregation_key: AggregationKey,
        block: int,
    ) -> AggregationWindow | None: ...

    def upsert_pending(self, window: AggregationWindow) -> None: ...

    def discard(self, window: AggregationWindow) -> None: ...


class ProcessingState(Protocol):
    @property
    def block(self) -> BlockProgressStore: ...

    @property
    def aggregation_windows(self) -> AggregationWindowStore: ...


class ProcessingStateProvider(Protocol):
    @property
    def state(self) -> ProcessingState: ...


class NotificationSink(Protocol):
    async def emit(self, notification: EventNotification) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparedNotifications:
    notifications: list[EventNotification]
    completed_window: AggregationWindow | None = None


class AggregationCoordinator:
    """Persist keyed event accumulators and flush them at processed block boundaries."""

    def __init__(
        self,
        *,
        storage: ProcessingStateProvider,
        notification_sink: NotificationSink,
        aggregators: tuple[EventAggregator, ...],
        metrics: AggregationMetricsMiddleware = DEFAULT_AGGREGATION_METRICS,
    ) -> None:
        self._storage = storage
        self._notification_sink = notification_sink
        self._metrics = metrics
        self._aggregators_by_group = {
            aggregator.group.name: aggregator for aggregator in aggregators
        }
        self._aggregators_by_event = {
            event_name: aggregator
            for aggregator in aggregators
            for event_name in aggregator.event_names
        }

    @property
    def _aggregation_windows(self) -> AggregationWindowStore:
        return self._storage.state.aggregation_windows

    @property
    def pending_window_count(self) -> int:
        return len(self._aggregation_windows.pending())

    async def handle_event(self, event: Event) -> None:
        prepared = await self._prepare(event)
        await self._emit_prepared(prepared)

    async def handle_block(self, block: int) -> None:
        await self._flush_ready_windows(block)

    async def resume_pending(self) -> None:
        await self._flush_ready_windows(self._storage.state.block.value)

    async def close(self) -> None:
        pass

    async def _prepare(self, event: Event) -> PreparedNotifications:
        aggregator = self._aggregators_by_event.get(event.event)
        if aggregator is None:
            return PreparedNotifications([EventNotification.from_event(event)])

        aggregation_key = aggregator.aggregation_key(event)
        window = self._aggregation_windows.pending_for(
            aggregator.group.name,
            aggregation_key,
            event.block,
        )
        if window is None:
            window = aggregator.window_for(event)

        window = window.with_event(event)
        self._aggregation_windows.upsert_pending(window)
        return PreparedNotifications([])

    async def _flush_ready_windows(self, block: int) -> None:
        ready_windows = sorted(
            (window for window in self._aggregation_windows.pending() if window.end_block <= block),
            key=lambda window: (
                window.end_block,
                window.start_block,
                window.group,
                window.aggregation_key.kind,
                window.aggregation_key.value,
            ),
        )
        for window in ready_windows:
            aggregator = self._aggregators_by_group.get(window.group)
            if aggregator is None:
                logger.warning(
                    "Discarding aggregation window without registered aggregator",
                    extra={
                        "group": window.group,
                        "aggregation_key": {
                            "kind": window.aggregation_key.kind,
                            "value": window.aggregation_key.value,
                        },
                        "event_names": sorted(window.event_names),
                    },
                )
                self._aggregation_windows.discard(window)
                continue
            prepared = await self._metrics.run(
                window.group,
                lambda window=window, aggregator=aggregator: self._aggregate_window(
                    window,
                    aggregator,
                ),
            )
            if prepared is not None:
                await self._emit_prepared(prepared)

    async def _aggregate_window(
        self,
        window: AggregationWindow,
        aggregator: EventAggregator,
    ) -> PreparedNotifications:
        return PreparedNotifications(
            aggregator.aggregate(window.events),
            completed_window=window,
        )

    async def _emit_prepared(self, prepared: PreparedNotifications) -> None:
        for notification in prepared.notifications:
            await self._notification_sink.emit(notification)
        if prepared.completed_window is not None:
            self._aggregation_windows.discard(prepared.completed_window)
