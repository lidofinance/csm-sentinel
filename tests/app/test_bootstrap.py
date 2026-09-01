import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationBuilder

from sentinel.app.application import SentinelApplication
from sentinel.app.bootstrap import (
    _bind_persistence_runtime,
    _persist_initial_checkpoint,
    _resolve_backfill_start_block,
    _run,
    _wait_for_subscription_start,
)
from sentinel.app.health import HealthState
from sentinel.app.runtime import BotRuntime
from sentinel.app.storage import RuntimeIdentity, create_persistence
from sentinel.module_types import ModuleType
from sentinel.services.subscription import ModuleRuntimeSupervisor


@pytest.mark.parametrize(
    ("configured_block", "persisted_block", "expected"),
    [
        (None, None, 0),
        (None, 0, 0),
        (None, "0x0", 0),
        (None, 42, 43),
        (100, None, 100),
        (0, 42, 0),
    ],
)
def test_resolve_backfill_start_block(
    configured_block: int | None,
    persisted_block: object | None,
    expected: int,
):
    assert _resolve_backfill_start_block(configured_block, persisted_block) == expected


@pytest.mark.asyncio
async def test_legacy_runtime_identity_is_persisted_before_periodic_update(tmp_path):
    persistence = create_persistence(tmp_path)
    application = (
        ApplicationBuilder()
        .application_class(SentinelApplication)
        .token("123:TEST")
        .persistence(persistence)
        .build()
    )
    identity = RuntimeIdentity(
        chain_id=1,
        module_address="0x1234",
        module_type=ModuleType.COMMUNITY,
    )
    application.bot_data["block"] = 25_600_000

    await _bind_persistence_runtime(application, identity)

    restarted_persistence = create_persistence(tmp_path)
    restarted_bot_data = await restarted_persistence.get_bot_data()
    assert restarted_bot_data["runtime_identity"] == identity.to_dict()
    assert restarted_bot_data["block"] == 25_600_000


@pytest.mark.asyncio
async def test_runtime_identity_mismatch_stops_before_polling_and_cleans_up():
    persisted_identity = RuntimeIdentity(
        chain_id=1,
        module_address="0x1234",
        module_type=ModuleType.COMMUNITY,
    )
    current_identity = RuntimeIdentity(
        chain_id=560_048,
        module_address="0x5678",
        module_type=ModuleType.CURATED,
    )
    updater = SimpleNamespace(start_polling=AsyncMock(), stop=AsyncMock())
    application = SimpleNamespace(
        updater=updater,
        bot_data={"runtime_identity": persisted_identity.to_dict(), "block": 25_600_000},
        persistence=SimpleNamespace(flush=AsyncMock()),
        initialize=AsyncMock(),
        update_persistence=AsyncMock(),
        start=AsyncMock(),
        stop=AsyncMock(),
        shutdown=AsyncMock(),
        add_error_handler=MagicMock(),
    )
    heartbeat_task = asyncio.create_task(asyncio.Event().wait())
    health = HealthState()
    module_supervisor = SimpleNamespace(
        cfg=SimpleNamespace(admin_ids=set(), block_from=None),
        request_shutdown=MagicMock(),
        close=AsyncMock(),
    )
    health_server = SimpleNamespace(stop=MagicMock())
    runtime = cast(
        BotRuntime,
        SimpleNamespace(
            application=application,
            module_supervisor=module_supervisor,
            job_context=MagicMock(),
            config=module_supervisor.cfg,
            module_adapter=MagicMock(),
            health=health,
            heartbeat_task=heartbeat_task,
            chain=SimpleNamespace(close=AsyncMock()),
            health_server=health_server,
            runtime_identity=current_identity,
        ),
    )

    try:
        with pytest.raises(RuntimeError, match="does not match configured runtime"):
            await _run(runtime)
    finally:
        if not heartbeat_task.done():
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    updater.start_polling.assert_not_awaited()
    updater.stop.assert_not_awaited()
    application.stop.assert_awaited_once()
    application.shutdown.assert_awaited_once()
    module_supervisor.close.assert_awaited_once()
    health_server.stop.assert_called_once()
    assert "does not match configured runtime" in (health.snapshot().fatal_error or "")


@pytest.mark.asyncio
async def test_initial_checkpoint_is_available_before_periodic_update(tmp_path):
    persistence = create_persistence(tmp_path)
    application = (
        ApplicationBuilder()
        .application_class(SentinelApplication)
        .token("123:TEST")
        .persistence(persistence)
        .build()
    )
    supervisor = MagicMock(spec=ModuleRuntimeSupervisor)

    async def establish_initial_checkpoint() -> int:
        application.bot_data["block"] = 25_600_000
        return 25_600_000

    supervisor.establish_initial_checkpoint = AsyncMock(side_effect=establish_initial_checkpoint)

    initial_checkpoint = await _persist_initial_checkpoint(application, supervisor)

    restarted_persistence = create_persistence(tmp_path)
    assert initial_checkpoint == 25_600_000
    assert (await restarted_persistence.get_bot_data())["block"] == 25_600_000


@pytest.mark.asyncio
async def test_subscription_start_propagates_early_supervisor_failure():
    never_subscribed = asyncio.Event()
    supervisor = SimpleNamespace(wait_until_subscribed=never_subscribed.wait)

    async def fail_supervisor():
        await asyncio.sleep(0)
        raise RuntimeError("subscription setup failed")

    supervisor_task = asyncio.create_task(fail_supervisor())

    with pytest.raises(RuntimeError, match="subscription setup failed"):
        await _wait_for_subscription_start(supervisor, supervisor_task)
