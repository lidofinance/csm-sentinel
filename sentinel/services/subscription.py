import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
import signal
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from web3 import AsyncWeb3

from sentinel.app.health import HealthState
from sentinel.chain import SharedChainConnection
from sentinel.config import Config
from sentinel.models import Block, Event, EventNotification
from sentinel.metrics.registry import DEFAULT_METRICS
from sentinel.modules.side_effects import ModuleEventSideEffects
from sentinel.rpc import Subscription
from sentinel.rpc_provider import RpcAvailabilityError
from sentinel.services.aggregation import AggregationCoordinator
from sentinel.services.digest import Digest, build_digests

logger = logging.getLogger(__name__)
logging.getLogger("web3.providers.WebSocketProvider").setLevel(logging.WARNING)
RPC_RECOVERY_RETRY_SECONDS = 1.0

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

    from sentinel.app.storage import BotStorage
    from sentinel.modules.base import BaseModuleAdapter
    from sentinel.modules.event_engine import EventMessageEngineBase


@dataclass(frozen=True, slots=True)
class ModuleRuntime:
    module_adapter: "BaseModuleAdapter"
    raw_subscription: Subscription
    storage: Callable[[], "BotStorage"]
    event_messages: "EventMessageEngineBase"
    event_side_effects: ModuleEventSideEffects
    aggregation: AggregationCoordinator
    digests: dict[str, Digest]

    async def handle_event(self, event: Event) -> None:
        await self.event_side_effects.process_event(event)
        for digest in self.digests.values():
            if await digest.handle_event(event):
                break
        else:
            await self.aggregation.handle_event(event)
        logger.info(
            "Event processed",
            extra={
                "event_name": event.event,
                "block": event.block,
                "transaction_index": event.transaction_index,
                "log_index": event.log_index,
            },
        )

    async def handle_block(self, block: Block) -> None:
        await self.aggregation.handle_block(block.number)
        self._advance_block(block.number)

    async def resume_pending_aggregations(self) -> None:
        await self.aggregation.resume_pending()

    def _advance_block(self, block_number: int) -> None:
        state = self.storage()
        state.block.update(max(state.block.value, block_number))


def build_module_runtime(
    w3,
    *,
    health: HealthState,
    module_adapter: "BaseModuleAdapter",
    storage: Callable[[], "BotStorage"],
    emit_notification: Callable[[EventNotification], Awaitable[None]],
    backfill_w3=None,
    catchup_until_block: int | None = None,
) -> ModuleRuntime:
    raw_subscription = Subscription(
        w3,
        health=health,
        backfill_w3=backfill_w3,
        module_adapter=module_adapter,
        ignore_subscription_events_until_block=catchup_until_block,
    )

    event_messages = module_adapter.build_event_messages()
    event_side_effects = ModuleEventSideEffects(module_adapter)
    aggregation = AggregationCoordinator(
        storage=storage,
        emit_notification=emit_notification,
        aggregators=module_adapter.event_aggregators(),
    )
    digests = build_digests(
        event_messages.event_handlers,
        lambda: storage().digests,
        emit_notification,
    )
    module_runtime = ModuleRuntime(
        module_adapter=module_adapter,
        raw_subscription=raw_subscription,
        storage=storage,
        event_messages=event_messages,
        event_side_effects=event_side_effects,
        aggregation=aggregation,
        digests=digests,
    )
    raw_subscription.add_event_consumer(module_runtime)
    raw_subscription.add_block_consumer(module_runtime)
    return module_runtime


class ModuleRuntimeSupervisor:
    """Own and replace the module-specific subscription runtime."""

    def __init__(
        self,
        subscription_w3_factory: Callable[[], AsyncWeb3],
        *,
        config: Config,
        chain: SharedChainConnection,
        health: HealthState,
        module_adapter: "BaseModuleAdapter",
        storage: Callable[[], "BotStorage"],
        emit_notification: Callable[[EventNotification], Awaitable[None]],
        backfill_w3=None,
    ) -> None:
        self._subscription_w3_factory = subscription_w3_factory
        self._backfill_w3 = backfill_w3
        self._config = config
        self._chain = chain
        self._health = health
        self._storage = storage
        self._emit_notification = emit_notification
        self._shutdown_requested = False
        self._module_runtime_restarted = asyncio.Event()
        self._catchup_until_block: int | None = None
        self._pending_replay_start_block: int | None = None
        self._signal_loop: "AbstractEventLoop | None" = None

        self._install_module_runtime(self._new_module_runtime(module_adapter))

    def _new_module_runtime(self, module_adapter: "BaseModuleAdapter") -> ModuleRuntime:
        return build_module_runtime(
            self._subscription_w3_factory(),
            health=self._health,
            backfill_w3=self._backfill_w3,
            module_adapter=module_adapter,
            storage=self._storage,
            emit_notification=self._emit_notification,
            catchup_until_block=self._catchup_until_block,
        )

    def _install_module_runtime(self, module_runtime: ModuleRuntime) -> None:
        self.module_runtime = module_runtime

    @property
    def raw_subscription(self) -> Subscription:
        return self.module_runtime.raw_subscription

    @property
    def event_messages(self) -> "EventMessageEngineBase":
        return self.module_runtime.event_messages

    @property
    def cfg(self):
        return self._config

    async def flush_digest(self, name: str, through_block: int) -> int:
        return await self.module_runtime.digests[name].flush_through(through_block)

    @property
    def digest_names(self) -> frozenset[str]:
        return frozenset(self.module_runtime.digests)

    def ensure_state_containers(self) -> None:
        self._storage()

    def setup_signal_handlers(self, loop: "AbstractEventLoop") -> None:
        self._signal_loop = loop
        loop.add_signal_handler(signal.SIGINT, self._signal_handler, loop)
        loop.add_signal_handler(signal.SIGTERM, self._signal_handler, loop)

    def _signal_handler(self, loop: "AbstractEventLoop") -> None:
        logger.info("Signal received, shutting down...")
        self.request_shutdown()
        loop.create_task(self.raw_subscription.stop())

    async def subscribe(self):
        while not self._shutdown_requested:
            if await self._subscribe_until_restarted_or_stopped():
                continue
            return

    async def _subscribe_until_restarted_or_stopped(self) -> bool:
        raw_subscription = self.raw_subscription
        await self.module_runtime.resume_pending_aggregations()
        self._module_runtime_restarted.clear()
        raw_subscription_task = asyncio.create_task(raw_subscription.subscribe())
        restart_task = asyncio.create_task(self._module_runtime_restarted.wait())
        subscribed_task: asyncio.Task[None] | None = None
        try:
            if self._pending_replay_start_block is not None:
                subscribed_task = asyncio.create_task(
                    raw_subscription.wait_until_subscribed(timeout=None)
                )
                done, _ = await asyncio.wait(
                    {raw_subscription_task, restart_task, subscribed_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if restart_task in done:
                    await raw_subscription.stop()
                    await raw_subscription_task
                    return True
                if raw_subscription_task in done:
                    return await self._handle_subscription_result(raw_subscription_task)
                await subscribed_task
                try:
                    await self._replay_pending_blocks_after_restart()
                except RpcAvailabilityError as exc:
                    if self._shutdown_requested:
                        return False
                    await self._restart_after_rpc_disconnect(exc)
                    return True

            done, _ = await asyncio.wait(
                {raw_subscription_task, restart_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if restart_task in done:
                await raw_subscription.stop()
                await raw_subscription_task
                return True
            return await self._handle_subscription_result(raw_subscription_task)
        finally:
            for task in (subscribed_task, raw_subscription_task, restart_task):
                if task is None or task.done():
                    continue
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _handle_subscription_result(
        self,
        raw_subscription_task: asyncio.Task[None],
    ) -> bool:
        try:
            await raw_subscription_task
        except RpcAvailabilityError as exc:
            if self._shutdown_requested:
                return False
            await self._restart_after_rpc_disconnect(exc)
            return True
        if self._shutdown_requested:
            return False
        await self._restart_after_rpc_disconnect(
            RuntimeError("Subscription stopped unexpectedly"),
            reason="listener_stopped",
        )
        return True

    async def _restart_after_rpc_disconnect(
        self, exc: BaseException, *, reason: str = "rpc_disconnect"
    ) -> None:
        previous_runtime = self.module_runtime
        replay_start_block = max(self._storage().block.value + 1, 1)
        logger.warning(
            "RPC connection lost; rebuilding subscription runtime and replaying from block %s: %s",
            replay_start_block,
            exc.__class__.__name__,
        )
        self._health.mark_catchup_started()
        previous_runtime.raw_subscription.request_shutdown()
        await previous_runtime.raw_subscription.abort()
        self._pending_replay_start_block = replay_start_block
        self._install_module_runtime(self._new_module_runtime(previous_runtime.module_adapter))
        DEFAULT_METRICS.chain.subscription_recovered(reason)

    async def _replay_pending_blocks_after_restart(self) -> None:
        replay_start_block = self._pending_replay_start_block
        if replay_start_block is None:
            return

        catchup_head = await self.raw_subscription.get_block_number()
        self._catchup_until_block = catchup_head
        try:
            await self.raw_subscription.replay_blocks(
                replay_start_block,
                end_block=catchup_head,
                suppress_live_events_until=catchup_head,
            )
            self._health.mark_catchup_complete()
        finally:
            self._catchup_until_block = None
        self._pending_replay_start_block = None

    async def wait_until_subscribed(self, *, timeout: float | None = None) -> None:
        await self.raw_subscription.wait_until_subscribed(timeout=timeout)

    async def get_block_number(self) -> int:
        return await self.raw_subscription.get_block_number()

    async def establish_initial_checkpoint(self) -> int:
        head = await self.get_block_number()
        checkpoint = self._storage().block
        checkpoint.update(max(head - 1, 0))
        return checkpoint.value

    async def catch_up_from(self, start_block: int) -> None:
        replay_start_block = start_block
        try:
            while not self._shutdown_requested:
                try:
                    catchup_head = await self.raw_subscription.get_block_number()
                    self._catchup_until_block = catchup_head
                    await self.raw_subscription.replay_blocks(
                        replay_start_block,
                        end_block=catchup_head,
                        suppress_live_events_until=catchup_head,
                    )
                    return
                except RpcAvailabilityError as exc:
                    if self._shutdown_requested:
                        return
                    replay_start_block = max(
                        replay_start_block,
                        self._storage().block.value + 1,
                    )
                    logger.warning(
                        "Catch-up RPC failed; retrying from block %s: %s",
                        replay_start_block,
                        exc,
                    )
                    await asyncio.sleep(RPC_RECOVERY_RETRY_SECONDS)
        finally:
            self._catchup_until_block = None

    def request_shutdown(self) -> None:
        self._shutdown_requested = True
        self.raw_subscription.request_shutdown()
        self._module_runtime_restarted.set()

    async def shutdown(self):
        await self.raw_subscription.shutdown()

    async def close(self) -> None:
        self.request_shutdown()
        await self.raw_subscription.stop()
        if self._backfill_w3 is not None and hasattr(self._backfill_w3.provider, "disconnect"):
            with suppress(Exception):
                await self._backfill_w3.provider.disconnect()
