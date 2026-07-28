import asyncio
import logging
from contextlib import suppress
from typing import Any, Protocol, cast

from web3 import AsyncWeb3, WebSocketProvider
from web3.utils.subscriptions import (
    LogsSubscription,
    LogsSubscriptionContext,
)

from sentinel.app.health import HealthState
from sentinel.config import Config, get_config
from sentinel.models import (
    Event,
    Block,
)
from sentinel.modules.base import ModuleAdapter
from sentinel.rpc_provider import (
    RpcAvailabilityError,
    RpcSubscriptionReconnectRequired,
)
from sentinel.web3_events import build_event_bindings, decode_event, log_filter_for_source
from sentinel.web3_event_log_reader import Web3EventLogReader

logger = logging.getLogger(__name__)
logging.getLogger("web3.providers.persistent.subscription_manager").setLevel(logging.WARNING)


class RawEventConsumer(Protocol):
    async def handle_event(self, event: Event) -> None: ...


class BlockConsumer(Protocol):
    async def handle_block(self, block: Block) -> None: ...


class Subscription:
    def __init__(
        self,
        w3: AsyncWeb3,
        *,
        health: HealthState,
        module_adapter: ModuleAdapter,
        backfill_w3: AsyncWeb3 | None = None,
        ignore_subscription_events_until_block: int | None = None,
    ):
        super().__init__()
        self._shutdown_event = asyncio.Event()
        self._subscriptions_started = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._subscriptions_detached = False
        self._w3 = w3
        self.cfg: Config = get_config()
        event_bindings = build_event_bindings(module_adapter)
        self._event_sources = event_bindings.event_sources
        self._abi_by_topics = event_bindings.abi_by_topics
        self._event_log_reader = Web3EventLogReader(
            backfill_w3 or w3,
            event_sources=self._event_sources,
            abi_by_topics=self._abi_by_topics,
            request_interval_seconds=self._request_interval_from_config(),
            stop_event=self._shutdown_event,
            provider_connected_message="Web3 backfill provider connected",
        )
        self._event_consumers: list[RawEventConsumer] = []
        self._block_consumers: list[BlockConsumer] = []
        self._ignore_subscription_events_until_block = ignore_subscription_events_until_block
        self._buffered_subscription_events: list[Event] | None = None
        self._health = health

    def add_event_consumer(self, consumer: RawEventConsumer) -> None:
        if consumer not in self._event_consumers:
            self._event_consumers.append(consumer)

    def remove_event_consumer(self, consumer: RawEventConsumer) -> None:
        if consumer in self._event_consumers:
            self._event_consumers.remove(consumer)

    def add_block_consumer(self, consumer: BlockConsumer) -> None:
        if consumer not in self._block_consumers:
            self._block_consumers.append(consumer)

    def remove_block_consumer(self, consumer: BlockConsumer) -> None:
        if consumer in self._block_consumers:
            self._block_consumers.remove(consumer)

    async def wait_until_subscribed(self, *, timeout: float | None = 10.0) -> None:
        """Wait until subscriptions are established (or raise on timeout)."""

        await asyncio.wait_for(self._subscriptions_started.wait(), timeout=timeout)

    async def get_block_number(self) -> int:
        """Return the latest block number from the persistent provider.

        Uses the main (subscription) provider rather than the backfill provider
        to avoid contention when backfill is running concurrently.
        """

        w3 = await self._connected_w3()
        return await w3.eth.get_block_number()

    async def _connected_w3(self) -> AsyncWeb3:
        if not await self._w3.provider.is_connected():
            await self._w3.provider.connect()
            logger.info("Web3 provider connected")
        return self._w3

    async def shutdown(self):
        await self._shutdown_event.wait()

    def request_shutdown(self) -> None:
        """Trigger shutdown (e.g., from a supervising task)."""

        self._shutdown_event.set()

    async def stop(self) -> None:
        """Request shutdown and detach active Web3 subscriptions."""

        await self._stop(unsubscribe=True)

    async def abort(self) -> None:
        """Stop a broken subscription without using its stale subscription IDs."""

        await self._stop(unsubscribe=False)

    async def _stop(self, *, unsubscribe: bool) -> None:
        """Stop this subscription and disconnect its persistent provider."""

        self.request_shutdown()
        async with self._stop_lock:
            if not self._subscriptions_detached:
                if unsubscribe:
                    try:
                        await self._w3.subscription_manager.unsubscribe_all()
                    except RpcAvailabilityError:
                        pass
                self._subscriptions_detached = True
        if hasattr(self._w3.provider, "disconnect"):
            with suppress(RpcAvailabilityError):
                await self._w3.provider.disconnect()
        self._health.mark_subscription_inactive()

    async def subscribe(self):
        if self._shutdown_event.is_set():
            return
        try:
            w3 = await self._connected_w3()
            await w3.subscription_manager.subscribe(self._build_log_subscriptions())
            logger.info("Subscriptions started")
            self._subscriptions_started.set()
            self._health.mark_subscription_active()

            await w3.subscription_manager.handle_subscriptions()
        except RpcAvailabilityError:
            self._health.mark_subscription_inactive()
            if self._shutdown_event.is_set():
                return
            raise

        if not self._shutdown_event.is_set():
            self._health.mark_subscription_inactive()
            raise RpcSubscriptionReconnectRequired.listener_stopped()

    async def replay_blocks(
        self,
        start_block: int,
        end_block: int | None = None,
        *,
        suppress_live_events_until: int | None = None,
    ):
        if suppress_live_events_until is not None:
            threshold = self._ignore_subscription_events_until_block
            self._ignore_subscription_events_until_block = max(
                threshold or 0,
                suppress_live_events_until,
            )
        previous_buffer = self._buffered_subscription_events
        self._buffered_subscription_events = []
        completed = False
        try:
            await self._replay_blocks(start_block, end_block=end_block)
            completed = True
        finally:
            if completed and not self._shutdown_event.is_set():
                await self._flush_buffered_subscription_events()
            self._buffered_subscription_events = previous_buffer

    async def _replay_blocks(self, start_block: int, end_block: int | None = None):
        w3 = await self._event_log_reader.connected_w3()
        if w3 is None:
            return
        end_block = end_block or await w3.eth.get_block_number()
        if start_block > end_block:
            logger.info("No blocks to process")
            logger.info("Backfill complete at block %s", end_block)
            return
        logger.info("Processing blocks from %s to %s", start_block, end_block)
        batch_size = self.cfg.block_batch_size
        for batch_start in range(start_block, end_block + 1, batch_size):
            batch_end = min(batch_start + batch_size - 1, end_block)
            logger.info(
                "Fetching logs for blocks %s-%s across %s sources",
                batch_start,
                batch_end,
                len(self._event_sources),
            )

            events = await self._event_log_reader.fetch_events(
                start_block=batch_start,
                end_block=batch_end,
            )

            if events is None:
                break

            for event in events:
                await self._emit_event(event)
                if self._shutdown_event.is_set():
                    break
                await asyncio.sleep(0)

            if self._shutdown_event.is_set():
                break
            await self._emit_block(Block(number=batch_end))
            self._health.mark_progress()
            logger.debug("Processed blocks up to %s", batch_end)
        if self._shutdown_event.is_set():
            logger.warning("Backfill interrupted before reaching block %s", end_block)
            return
        logger.info("Backfill complete at block %s", end_block)

    def _request_interval_from_config(self) -> float | None:
        rps_limit = self.cfg.process_blocks_requests_per_second
        return (1 / rps_limit) if rps_limit else None

    async def _handle_event_log_subscription(self, context: LogsSubscriptionContext):
        # web3 stubs type `context.result` too broadly; treat as a log receipt-like mapping.
        result = cast(dict[str, Any], context.result)
        event_topic = result["topics"][0]
        event_abi = self._abi_by_topics.get(event_topic)
        if not event_abi:
            return
        event = decode_event(self._w3, event_abi, result)
        if hasattr(context, "predicate") and not context.predicate(event):
            return
        self._health.mark_progress()
        await self._emit_subscription_event(event)

    def _build_log_subscriptions(self) -> list[LogsSubscription]:
        subscriptions = []
        for source in self._event_sources:
            filter_params = log_filter_for_source(source, self._abi_by_topics)
            if filter_params is None:
                continue
            handler_context = {}
            if source.predicate is not None:
                handler_context["predicate"] = source.predicate

            kwargs: dict[str, Any] = {
                "handler": self._handle_event_log_subscription,
                **filter_params,
            }
            if handler_context:
                kwargs["handler_context"] = handler_context

            subscriptions.append(LogsSubscription(**kwargs))
        return subscriptions

    async def _emit_event(self, event: Event):
        if self._shutdown_event.is_set():
            return
        for consumer in self._event_consumers:
            if self._shutdown_event.is_set():
                return
            await consumer.handle_event(event)

    async def _emit_block(self, block: Block):
        if self._shutdown_event.is_set():
            return
        for consumer in self._block_consumers:
            if self._shutdown_event.is_set():
                return
            await consumer.handle_block(block)

    async def _emit_subscription_event(self, event: Event):
        """Handle a log event received via the live subscription."""

        if self._shutdown_event.is_set():
            return
        threshold = self._ignore_subscription_events_until_block
        if threshold is not None and event.block <= threshold:
            return
        if self._buffered_subscription_events is not None:
            self._buffered_subscription_events.append(event)
            return
        await self._emit_event(event)

    async def _flush_buffered_subscription_events(self) -> None:
        while self._buffered_subscription_events:
            events = sorted(
                self._buffered_subscription_events,
                key=lambda event: (event.block, event.transaction_index, event.log_index),
            )
            self._buffered_subscription_events.clear()
            for event in events:
                if self._shutdown_event.is_set():
                    return
                await self._emit_event(event)


class LoggingConsumer:
    async def handle_event(self, event: Event):
        logger.warning("Event %s emitted with data: %s", event.event, event.args)

    async def handle_block(self, block: Block):
        logger.warning("Current block number: %s", block.number)


if __name__ == "__main__":
    from sentinel.app.module_adapter import build_module_adapter_from_config
    from sentinel.chain import SharedChainConnection

    cfg = get_config()
    provider = AsyncWeb3(WebSocketProvider(cfg.web3_socket_providers[0]))
    module_adapter = build_module_adapter_from_config(
        cfg,
        provider,
        SharedChainConnection(provider),
    )

    subscription = Subscription(
        provider,
        health=HealthState(),
        module_adapter=module_adapter,
    )
    logging_consumer = LoggingConsumer()
    subscription.add_event_consumer(logging_consumer)
    subscription.add_block_consumer(logging_consumer)

    asyncio.run(subscription.subscribe())
