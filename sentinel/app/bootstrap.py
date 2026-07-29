import asyncio
import logging
from pathlib import Path
from contextlib import suppress
from typing import cast

from telegram.ext import AIORateLimiter, ApplicationBuilder, ContextTypes
from sentinel.app.application import SentinelApplication
from sentinel.app.contracts import discover_contract_addresses, log_discovered_addresses
from sentinel.app.context import BotContext
from sentinel.app.health import HealthServer, HealthState
from sentinel.app.module_adapter import build_module_adapter_from_config
from sentinel.app.runtime import BotRuntime
from sentinel.app.storage import create_persistence
from sentinel.app.telegram_adapters import (
    TelegramNotificationHandler,
    TelegramNotificationSink,
    TelegramProcessingStateProvider,
)
from sentinel.chain import SharedChainConnection
from sentinel.config import load_config_from_env, set_config
from sentinel.utils import normalize_block_number
from sentinel.handlers.errors import error_handler, build_error_callback
from sentinel.services.subscription import (
    ModuleRuntimeSupervisor,
)
from sentinel.jobs import JobContext
from sentinel.rpc_provider import (
    FallbackAsyncWeb3,
    FallbackRequestProvider,
    FallbackSubscriptionProvider,
    RpcEndpointPool,
)
from sentinel.metrics import DEFAULT_METRICS, RpcMetricsMiddleware

logger = logging.getLogger(__name__)


def _resolve_backfill_start_block(
    configured_block: int | None,
    persisted_block: object | None,
) -> int:
    if configured_block is not None:
        return configured_block
    if persisted_block is None:
        return 0

    checkpoint = normalize_block_number(persisted_block)
    return checkpoint + 1 if checkpoint > 0 else 0


async def create_runtime() -> BotRuntime:
    env_cfg = load_config_from_env()
    health = HealthState()
    health_server = HealthServer(
        health,
        host=env_cfg.healthcheck_host,
        port=env_cfg.healthcheck_port,
    )
    health_server.start()
    heartbeat_task = asyncio.create_task(health.heartbeat_loop())

    rpc_endpoint_pool = RpcEndpointPool(env_cfg.web3_socket_providers)
    reads_provider = FallbackRequestProvider(
        rpc_endpoint_pool, role="reads", observer=DEFAULT_METRICS.rpc
    )
    backfill_ws_provider = FallbackRequestProvider(
        rpc_endpoint_pool, role="backfill", observer=DEFAULT_METRICS.rpc
    )
    rpc_provider = FallbackAsyncWeb3(reads_provider)
    rpc_provider.middleware_onion.inject(RpcMetricsMiddleware, layer=0)
    backfill_provider = FallbackAsyncWeb3(backfill_ws_provider)
    backfill_provider.middleware_onion.inject(RpcMetricsMiddleware, layer=0)
    subscription_provider: FallbackSubscriptionProvider | None = None

    def create_subscription_w3() -> FallbackAsyncWeb3:
        nonlocal subscription_provider
        subscription_provider = FallbackSubscriptionProvider(
            rpc_endpoint_pool,
            role="subscription",
            observer=DEFAULT_METRICS.rpc,
        )
        subscription_w3 = FallbackAsyncWeb3(subscription_provider)
        subscription_w3.middleware_onion.inject(RpcMetricsMiddleware, layer=0)
        return subscription_w3

    try:
        await reads_provider.validate_endpoint_chain_ids()
        addresses = await discover_contract_addresses(rpc_provider, env_cfg.module_address)
        log_discovered_addresses(addresses)
        cfg = env_cfg.resolve(addresses)
        set_config(cfg)

        if cfg.token is None:
            raise RuntimeError("TOKEN must be configured")

        storage_path = Path(cfg.filestorage_path)
        storage_path.mkdir(parents=True, exist_ok=True)

        persistence = create_persistence(storage_path)

        context_types = ContextTypes(context=BotContext)

        application = cast(
            SentinelApplication,
            ApplicationBuilder()
            .application_class(SentinelApplication)
            .token(cfg.token)
            .context_types(context_types)
            .persistence(persistence)
            .rate_limiter(AIORateLimiter(max_retries=5))
            .build(),
        )

        chain = SharedChainConnection(rpc_provider)
        module_adapter = build_module_adapter_from_config(cfg, rpc_provider, chain)

        module_supervisor = ModuleRuntimeSupervisor(
            create_subscription_w3,
            config=cfg,
            chain=chain,
            health=health,
            module_adapter=module_adapter,
            storage=TelegramProcessingStateProvider(application),
            notification_sink=TelegramNotificationSink(application),
            backfill_w3=backfill_provider,
        )
        notification_handler = TelegramNotificationHandler(
            application,
            lambda: module_supervisor.event_messages,
        )
        job_context = JobContext(module_supervisor)

        runtime = BotRuntime(
            application=application,
            module_supervisor=module_supervisor,
            notification_handler=notification_handler,
            job_context=job_context,
            chain=chain,
            health=health,
            health_server=health_server,
            heartbeat_task=heartbeat_task,
        )
        application.attach_runtime(runtime)
        return runtime
    except BaseException as exc:
        health.mark_fatal_error(exc)
        health_server.stop()
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        for provider in (
            subscription_provider,
            reads_provider,
            backfill_ws_provider,
        ):
            if provider is None:
                continue
            with suppress(Exception):
                await provider.disconnect()
        raise


async def _run(runtime: BotRuntime) -> None:
    application = runtime.application
    module_supervisor = runtime.module_supervisor
    job_context = runtime.job_context
    cfg = runtime.config

    updater = application.updater
    if updater is None:
        raise RuntimeError("Application updater is not configured; cannot start polling")

    await application.initialize()
    await application.start()
    application.add_error_handler(error_handler)

    heartbeat_task = runtime.heartbeat_task
    module_supervisor_task: asyncio.Task[None] | None = None
    try:
        persisted_block = application.bot_data.get("block")
        module_supervisor.ensure_state_containers()
        runtime.health.mark_warmup_started()
        try:
            await runtime.module_adapter.warm_up()
        except Exception as exc:
            runtime.health.mark_warmup_failed(exc)
            logger.warning("Failed to warm up module adapter cache", exc_info=True)
        else:
            runtime.health.mark_warmup_complete()

        application.bot_data["admin_ids"] = cfg.admin_ids

        block_from = _resolve_backfill_start_block(
            cfg.block_from,
            persisted_block,
        )

        logger.info(
            "Bot started. Backfill start block: %s",
            block_from,
        )

        error_callback = build_error_callback(application)
        await updater.start_polling(error_callback=error_callback)
        runtime.health.mark_polling_started()
        module_supervisor.setup_signal_handlers(asyncio.get_running_loop())

        # Start the live subscription first, then backfill up to a post-subscribe head.
        # This avoids missing blocks mined while historical catch-up is running.
        module_supervisor_task = asyncio.create_task(module_supervisor.subscribe())
        await _wait_for_subscription_start(module_supervisor, module_supervisor_task)
        runtime.health.mark_startup_complete()

        if block_from != 0:
            runtime.health.mark_catchup_started()
            await module_supervisor.catch_up_from(block_from)
            runtime.health.mark_catchup_complete()
        else:
            live_head = await module_supervisor.checkpoint_current_head()
            logger.info(
                "Historical backfill skipped. Starting from live head: %s",
                live_head,
            )

        await job_context.schedule(application)

        await module_supervisor_task
    except asyncio.CancelledError:  # pragma: no cover - shutdown guard
        pass
    except Exception as exc:
        runtime.health.mark_fatal_error(exc)
        raise
    finally:
        runtime.health.mark_shutdown_requested()
        # Ensure shutdown never hangs on unexpected failures (e.g., subscription startup timeouts).
        module_supervisor.request_shutdown()
        if module_supervisor_task is not None and not module_supervisor_task.done():
            module_supervisor_task.cancel()
            with suppress(asyncio.CancelledError):
                await module_supervisor_task
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await module_supervisor.close()
        await updater.stop()
        await application.stop()
        await runtime.chain.close()
        await application.shutdown()
        runtime.health_server.stop()


async def run(runtime: BotRuntime) -> None:
    await _run(runtime)


async def _wait_for_subscription_start(
    module_supervisor: ModuleRuntimeSupervisor,
    supervisor_task: asyncio.Task[None],
) -> None:
    subscribed_task = asyncio.create_task(module_supervisor.wait_until_subscribed())
    try:
        done, _ = await asyncio.wait(
            {subscribed_task, supervisor_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if supervisor_task in done:
            await supervisor_task
            raise RuntimeError("Subscription supervisor stopped before startup completed")
        await subscribed_task
    finally:
        if not subscribed_task.done():
            subscribed_task.cancel()
            with suppress(asyncio.CancelledError):
                await subscribed_task
