from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from sentinel.app.storage import BotStorage
from sentinel.app.secrets import SecretBundle
from sentinel.jobs import JobContext, ALERT_INTERVAL_MINUTES
from sentinel.modules.community.texts import CommunityTexts
from sentinel.modules.community.texts import NO_NEW_BLOCKS_ADMIN_ALERT


class StubBot:
    def __init__(self):
        self.sent_messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent_messages.append((chat_id, text))


def _make_context(admin_ids: set[int], block: int, bot: StubBot) -> SimpleNamespace:
    bot_storage = BotStorage({"block": block})
    runtime = SimpleNamespace(
        config=SimpleNamespace(admin_ids=admin_ids),
        module_adapter=SimpleNamespace(texts=CommunityTexts),
    )
    return SimpleNamespace(bot_storage=bot_storage, runtime=runtime, bot=bot)


def _make_subscription(chain_head: int = 0) -> SimpleNamespace:
    sub = SimpleNamespace()
    sub.get_block_number = AsyncMock(return_value=chain_head)
    return sub


@pytest.mark.asyncio
async def test_scheduled_jobs_have_distinct_names(tmp_path: Path):
    app = SimpleNamespace(job_queue=SimpleNamespace(run_repeating=Mock()))
    job_context = JobContext(
        _make_subscription(),
        secret_bundle=SecretBundle(tmp_path / "secrets.env", 1),
    )

    await job_context.schedule(app)

    assert [call.kwargs["name"] for call in app.job_queue.run_repeating.call_args_list] == [
        "block_processing_check",
        "chain_head_poll",
        "secret_rotation_check",
    ]


@pytest.mark.asyncio
async def test_secret_rotation_job_stops_subscription_on_new_version(tmp_path: Path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("SECRET_VERSION=2\nTOKEN=new\n")
    supervisor = SimpleNamespace(
        request_shutdown=Mock(),
        raw_subscription=SimpleNamespace(stop=AsyncMock()),
    )
    job = SimpleNamespace(schedule_removal=Mock())
    context = SimpleNamespace(
        job=job,
        runtime=SimpleNamespace(module_supervisor=supervisor),
    )
    job_context = JobContext(
        _make_subscription(),
        secret_bundle=SecretBundle(secrets, 1),
    )

    assert await job_context._check_secret_rotation(context) is True
    job.schedule_removal.assert_called_once_with()
    supervisor.request_shutdown.assert_called_once_with()
    supervisor.raw_subscription.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_secret_rotation_job_ignores_loaded_version(tmp_path: Path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("SECRET_VERSION=1\nTOKEN=current\n")
    supervisor = SimpleNamespace(
        request_shutdown=Mock(),
        raw_subscription=SimpleNamespace(stop=AsyncMock()),
    )
    context = SimpleNamespace(runtime=SimpleNamespace(module_supervisor=supervisor))
    job_context = JobContext(
        _make_subscription(),
        secret_bundle=SecretBundle(secrets, 1),
    )

    assert await job_context._check_secret_rotation(context) is True
    supervisor.request_shutdown.assert_not_called()
    supervisor.raw_subscription.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_alert_when_chain_head_not_polled():
    bot = StubBot()
    context = _make_context({1}, block=100, bot=bot)
    job_context = JobContext(_make_subscription())

    # chain_head is 0 (not yet polled) -> no alert
    await job_context.callback_block_processing_check(context)
    assert not bot.sent_messages


@pytest.mark.asyncio
async def test_no_alert_on_first_check_after_poll():
    bot = StubBot()
    chain_head = 1000
    context = _make_context({1}, block=0, bot=bot)

    sub = _make_subscription(chain_head)
    job_context = JobContext(sub)
    job_context._chain_head = chain_head

    await job_context.callback_block_processing_check(context)
    assert job_context._last_checked_chain_head == chain_head
    assert not bot.sent_messages


@pytest.mark.asyncio
async def test_alert_when_chain_head_does_not_advance():
    admin_ids = {1, 99}
    bot = StubBot()
    chain_head = 1000
    context = _make_context(admin_ids, block=0, bot=bot)

    sub = _make_subscription(chain_head)
    job_context = JobContext(sub)
    job_context._last_checked_chain_head = chain_head
    job_context._chain_head = chain_head

    await job_context.callback_block_processing_check(context)
    expected_message = NO_NEW_BLOCKS_ADMIN_ALERT.format(
        minutes=ALERT_INTERVAL_MINUTES,
        block=chain_head,
    )
    assert sorted(bot.sent_messages) == sorted((aid, expected_message) for aid in admin_ids)


@pytest.mark.asyncio
async def test_alert_only_once_until_chain_head_advances():
    admin_ids = {1}
    bot = StubBot()
    chain_head = 1000
    context = _make_context(admin_ids, block=0, bot=bot)

    sub = _make_subscription(chain_head)
    job_context = JobContext(sub)
    job_context._last_checked_chain_head = chain_head
    job_context._chain_head = chain_head

    # First check -> alert
    await job_context.callback_block_processing_check(context)
    assert len(bot.sent_messages) == 1

    # Second check, still no progress -> no additional alert
    job_context._chain_head = chain_head
    await job_context.callback_block_processing_check(context)
    assert len(bot.sent_messages) == 1

    # Chain head advances -> reset
    job_context._chain_head = chain_head + 10
    await job_context.callback_block_processing_check(context)
    assert len(bot.sent_messages) == 1
    assert not job_context._alerted

    # Stalls again -> new alert
    await job_context.callback_block_processing_check(context)
    assert len(bot.sent_messages) == 2


@pytest.mark.asyncio
async def test_poll_chain_head():
    sub = _make_subscription(999_999)
    job_context = JobContext(sub)
    context = _make_context({1}, block=100, bot=StubBot())

    await job_context._poll_chain_head(context)
    assert job_context._chain_head == 999_999
    assert context.bot_storage.block.value == 100
    sub.get_block_number.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_chain_head_failure_does_not_crash():
    sub = _make_subscription()
    sub.get_block_number = AsyncMock(side_effect=Exception("connection lost"))
    job_context = JobContext(sub)
    job_context._chain_head = 42

    await job_context._poll_chain_head(None)
    # chain_head should remain unchanged after failure
    assert job_context._chain_head == 42
