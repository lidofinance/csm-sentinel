import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import suppress
from typing import Any, TypeVar, cast

from web3 import AsyncWeb3
from web3.types import FilterParams

from sentinel.models import Event
from sentinel.modules.base import EventSource
from sentinel.web3_events import decode_event, log_filter_for_source

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Web3EventLogReader:
    """Shared Web3 log reader for backfill streams and random-access history."""

    def __init__(
        self,
        w3: AsyncWeb3,
        *,
        event_sources: tuple[EventSource, ...] = (),
        abi_by_topics: dict | None = None,
        request_interval_seconds: float | None,
        block_batch_size: int | None = None,
        stop_event: asyncio.Event | None = None,
        provider_connected_message: str = "Web3 event log reader provider connected",
    ) -> None:
        if block_batch_size is not None and block_batch_size < 1:
            raise ValueError("block_batch_size must be at least 1")
        self._w3 = w3
        self._event_sources = event_sources
        self._abi_by_topics = abi_by_topics or {}
        self._request_interval_seconds = request_interval_seconds
        self._block_batch_size = block_batch_size
        self._last_request_ts: float | None = None
        self._stop_event = stop_event
        self._provider_connected_message = provider_connected_message

    async def connected_w3(self) -> AsyncWeb3 | None:
        if self._is_stopped():
            return None
        if not await self._w3.provider.is_connected():
            stopped, _ = await self._run_or_stop(self._w3.provider.connect)
            if stopped:
                return None
            logger.info(self._provider_connected_message)
        return self._w3

    async def _run_or_stop(
        self,
        operation: Callable[[], Awaitable[T]],
    ) -> tuple[bool, T | None]:
        if self._is_stopped():
            return True, None
        if self._stop_event is None:
            return False, await operation()

        operation_task = asyncio.ensure_future(operation())
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {operation_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                if not operation_task.done():
                    operation_task.cancel()
                try:
                    await operation_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("Backfill RPC failed during shutdown", exc_info=True)
                return True, None

            return False, await operation_task
        finally:
            if not stop_task.done():
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task

    async def fetch_events(
        self,
        *,
        start_block: int,
        end_block: int,
    ) -> list[Event] | None:
        w3 = await self.connected_w3()
        if w3 is None:
            return None

        events: list[Event] = []
        for source in self._event_sources:
            source_filter = log_filter_for_source(source, self._abi_by_topics)
            if source_filter is None:
                continue

            for batch_start, batch_end in self._block_ranges(start_block, end_block):
                filter_params = cast(FilterParams, dict(source_filter))
                filter_params["fromBlock"] = batch_start
                filter_params["toBlock"] = batch_end

                logs = await self.get_logs(
                    w3=w3,
                    filter_params=filter_params,
                )
                if logs is None:
                    return None

                for log in logs:
                    event_topic = log["topics"][0]
                    event_abi = self._abi_by_topics.get(event_topic)
                    if event_abi is None:
                        continue
                    if (
                        source.event_names is not None
                        and event_abi["name"] not in source.event_names
                    ):
                        continue
                    event = decode_event(w3, event_abi, log)
                    if source.predicate is not None and not source.predicate(event):
                        continue
                    events.append(event)

        return sorted(
            events,
            key=lambda event: (event.block, event.transaction_index, event.log_index),
        )

    async def get_logs(
        self,
        *,
        w3: AsyncWeb3,
        filter_params: FilterParams,
    ) -> list[Any] | None:
        if self._is_stopped() or await self.throttle():
            return None

        stopped, logs = await self._run_or_stop(lambda: w3.eth.get_logs(filter_params))
        if stopped:
            return None
        assert logs is not None
        return logs

    def _block_ranges(self, start_block: int, end_block: int) -> Iterator[tuple[int, int]]:
        batch_size = self._block_batch_size or max(end_block - start_block + 1, 1)
        for batch_start in range(start_block, end_block + 1, batch_size):
            yield batch_start, min(batch_start + batch_size - 1, end_block)

    async def throttle(self) -> bool:
        if self._request_interval_seconds is None:
            return False
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._last_request_ts is not None:
            elapsed = now - self._last_request_ts
            sleep_for = self._request_interval_seconds - elapsed
            if sleep_for > 0:
                logger.debug("Throttling event log reader requests for %.3fs", sleep_for)
                if await self._sleep(sleep_for):
                    return True
                now = loop.time()
        self._last_request_ts = now
        return False

    def _is_stopped(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    async def _sleep(self, delay_seconds: float) -> bool:
        if self._stop_event is None:
            await asyncio.sleep(delay_seconds)
            return False
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay_seconds)
        except TimeoutError:
            return False
        return True
