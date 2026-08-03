import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import replace
import signal
from typing import TYPE_CHECKING, Callable

from web3 import AsyncWeb3

from sentinel.app.contracts import discover_contract_addresses, log_discovered_addresses
from sentinel.app.health import HealthState
from sentinel.app.module_adapter import build_module_adapter_from_config
from sentinel.chain import SharedChainConnection
from sentinel.config import Config
from sentinel.config import set_config
from sentinel.models import Block, Event
from sentinel.metrics.registry import DEFAULT_METRICS
from sentinel.modules.community.adapter import CommunityModuleAdapter
from sentinel.modules.side_effects import ModuleEventSideEffects
from sentinel.rpc import Subscription
from sentinel.rpc_provider import RpcAvailabilityError
from sentinel.services.aggregation import (
    AggregationCoordinator,
    NotificationSink,
    ProcessingStateProvider,
)
from sentinel.services.event_history import Web3EventHistory

logger = logging.getLogger(__name__)
logging.getLogger("web3.providers.WebSocketProvider").setLevel(logging.WARNING)
RPC_RECOVERY_RETRY_SECONDS = 1.0

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

    from sentinel.modules.base import ModuleAdapter
    from sentinel.notifications import EventMessageEngine


class CsmVersionUpgradeRequired(Exception):
    def __init__(self, *, block: int, version: int) -> None:
        self.block = block
        self.version = version
        super().__init__(f"CSM runtime must be rebuilt for version {version}")


@dataclass(frozen=True, slots=True)
class ModuleRuntime:
    module_adapter: "ModuleAdapter"
    raw_subscription: Subscription
    storage: ProcessingStateProvider
    event_messages: "EventMessageEngine"
    event_side_effects: ModuleEventSideEffects
    aggregation: AggregationCoordinator

    async def handle_event(self, event: Event) -> None:
        if _is_csm_version_upgrade_event(self.module_adapter, event):
            raise CsmVersionUpgradeRequired(
                block=event.block,
                version=int(event.args["version"]),
            )
        await self.event_side_effects.process_event(event)
        await self.aggregation.handle_event(event)
        self._advance_block(event.block)

    async def handle_block(self, block: Block) -> None:
        self._advance_block(block.number)

    def resume_pending_aggregations(self) -> None:
        self.aggregation.resume_pending()

    async def close(self) -> None:
        await self.aggregation.close()

    def _advance_block(self, block_number: int) -> None:
        state = self.storage.state
        state.block.update(max(state.block.value, block_number))


def _is_csm_version_upgrade_event(module_adapter: "ModuleAdapter", event: Event) -> bool:
    if not isinstance(module_adapter, CommunityModuleAdapter):
        return False
    if module_adapter.csm_version >= 3:
        return False
    if event.event != "Initialized":
        return False
    if int(event.args.get("version", 0)) != 3:
        return False
    return event.address.lower() == module_adapter.addresses.module.lower()


def build_module_runtime(
    w3,
    *,
    health: HealthState,
    module_adapter: "ModuleAdapter",
    storage: ProcessingStateProvider,
    notification_sink: NotificationSink,
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
    event_history = Web3EventHistory(
        backfill_w3 or w3,
        module_adapter=module_adapter,
    )
    aggregation = AggregationCoordinator(
        storage=storage,
        event_history=event_history,
        block_reader=raw_subscription,
        notification_sink=notification_sink,
        aggregators=module_adapter.event_aggregators(),
    )
    module_runtime = ModuleRuntime(
        module_adapter=module_adapter,
        raw_subscription=raw_subscription,
        storage=storage,
        event_messages=event_messages,
        event_side_effects=event_side_effects,
        aggregation=aggregation,
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
        module_adapter: "ModuleAdapter",
        storage: ProcessingStateProvider,
        notification_sink: NotificationSink,
        backfill_w3=None,
    ) -> None:
        self._subscription_w3_factory = subscription_w3_factory
        self._backfill_w3 = backfill_w3
        self._config = config
        self._chain = chain
        self._health = health
        self._storage = storage
        self._notification_sink = notification_sink
        self._shutdown_requested = False
        self._module_runtime_restarted = asyncio.Event()
        self._catchup_until_block: int | None = None
        self._pending_replay_start_block: int | None = None
        self._signal_loop: "AbstractEventLoop | None" = None

        self._install_module_runtime(self._new_module_runtime(module_adapter))

    def _new_module_runtime(self, module_adapter: "ModuleAdapter") -> ModuleRuntime:
        return build_module_runtime(
            self._subscription_w3_factory(),
            health=self._health,
            backfill_w3=self._backfill_w3,
            module_adapter=module_adapter,
            storage=self._storage,
            notification_sink=self._notification_sink,
            catchup_until_block=self._catchup_until_block,
        )

    def _install_module_runtime(self, module_runtime: ModuleRuntime) -> None:
        self.module_runtime = module_runtime

    @property
    def raw_subscription(self) -> Subscription:
        return self.module_runtime.raw_subscription

    @property
    def event_messages(self) -> "EventMessageEngine":
        return self.module_runtime.event_messages

    @property
    def cfg(self):
        return self._config

    async def _handle_module_upgrade(
        self,
        upgrade: CsmVersionUpgradeRequired,
    ) -> int:
        previous_runtime = self.module_runtime
        replay_start_block = max(upgrade.block, 1)
        checkpoint = replay_start_block - 1

        logger.info(
            "CSM v%s upgrade detected at block %s; rebuilding module runtime",
            upgrade.version,
            upgrade.block,
        )
        previous_runtime.raw_subscription.request_shutdown()
        self._storage.state.block.update(checkpoint)
        await previous_runtime.close()

        contract_addresses = await discover_contract_addresses(
            self._chain.w3,
            self._config.contract_addresses.module,
        )
        log_discovered_addresses(contract_addresses)
        cfg = replace(self._config, contract_addresses=contract_addresses)
        module_adapter = build_module_adapter_from_config(cfg, self._chain.w3, self._chain)
        if (
            isinstance(module_adapter, CommunityModuleAdapter)
            and module_adapter.csm_version < upgrade.version
        ):
            raise RuntimeError(
                f"Discovered CSM v{module_adapter.csm_version}; "
                f"expected at least v{upgrade.version}"
            )

        set_config(cfg)
        self._config = cfg
        self._install_module_runtime(self._new_module_runtime(module_adapter))
        self._module_runtime_restarted.set()

        logger.info("Module runtime rebuilt after CSM v%s upgrade", upgrade.version)
        return replay_start_block

    def ensure_state_containers(self) -> None:
        self._storage.state

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
        self.module_runtime.resume_pending_aggregations()
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
                    with suppress(CsmVersionUpgradeRequired):
                        await raw_subscription_task
                    return True
                if raw_subscription_task in done:
                    return await self._handle_subscription_result(
                        raw_subscription, raw_subscription_task
                    )
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
                with suppress(CsmVersionUpgradeRequired):
                    await raw_subscription_task
                return True
            return await self._handle_subscription_result(raw_subscription, raw_subscription_task)
        finally:
            for task in (subscribed_task, raw_subscription_task, restart_task):
                if task is None or task.done():
                    continue
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _handle_subscription_result(
        self,
        raw_subscription: Subscription,
        raw_subscription_task: asyncio.Task[None],
    ) -> bool:
        try:
            await raw_subscription_task
        except CsmVersionUpgradeRequired as upgrade:
            replay_start_block = await self._handle_module_upgrade(upgrade)
            self._pending_replay_start_block = replay_start_block
            await raw_subscription.stop()
            return True
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
        replay_start_block = max(self._storage.state.block.value, 1)
        logger.warning(
            "RPC connection lost; rebuilding subscription runtime and replaying from block %s: %s",
            replay_start_block,
            exc.__class__.__name__,
        )
        self._health.mark_catchup_started()
        previous_runtime.raw_subscription.request_shutdown()
        await previous_runtime.raw_subscription.abort()
        await previous_runtime.close()
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

    async def checkpoint_current_head(self) -> int:
        head = await self.get_block_number()
        checkpoint = self._storage.state.block
        checkpoint.update(max(checkpoint.value, head))
        return head

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
                except CsmVersionUpgradeRequired as upgrade:
                    replay_start_block = await self._handle_module_upgrade(upgrade)
                    await self.wait_until_subscribed()
                except RpcAvailabilityError as exc:
                    if self._shutdown_requested:
                        return
                    replay_start_block = max(
                        replay_start_block,
                        self._storage.state.block.value + 1,
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
        await self.module_runtime.close()
        await self.raw_subscription.stop()
        if self._backfill_w3 is not None and hasattr(self._backfill_w3.provider, "disconnect"):
            with suppress(Exception):
                await self._backfill_w3.provider.disconnect()
