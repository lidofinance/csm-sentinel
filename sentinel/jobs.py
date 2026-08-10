import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

from telegram.ext import Application

from sentinel.app.secrets import (
    SECRET_WATCH_INTERVAL_SECONDS,
    SecretBundle,
    read_secret_version,
)
from sentinel.app.storage import BotStorage
from sentinel.config import Config
from sentinel.metrics.jobs import JobMetricsMiddleware
from sentinel.metrics.registry import DEFAULT_METRICS
from sentinel.services.digest import DigestGroups

logger = logging.getLogger(__name__)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

if TYPE_CHECKING:
    from sentinel.app.context import BotContext


CHAIN_HEAD_POLL_INTERVAL_SECONDS = 5 * 60  # 5 minutes
ALERT_INTERVAL_MINUTES = 30
DEFAULT_JOB_METRICS = JobMetricsMiddleware(DEFAULT_METRICS.jobs)
DEPOSIT_DIGEST_JOB_NAME = "deposit_digest"
SCHEDULED_DIGESTS = frozenset({DigestGroups.DEPOSITED_SIGNING_KEYS})


class JobContext:
    _alerted: bool = False

    def __init__(
        self,
        get_block_number: Callable[[], Awaitable[int]],
        *,
        config: Config,
        digest_names: frozenset[str],
        secret_bundle: SecretBundle | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._get_block_number = get_block_number
        self._config = config
        if digest_names != SCHEDULED_DIGESTS:
            raise RuntimeError(
                "Digest jobs do not match registered digests: "
                f"registered={sorted(digest_names)}, scheduled={sorted(SCHEDULED_DIGESTS)}"
            )
        self._secret_bundle = secret_bundle
        self._now = now or (lambda: datetime.now(UTC))
        self._metrics = DEFAULT_JOB_METRICS
        self._deposit_digest_lock = asyncio.Lock()
        self._chain_head: int = 0
        self._last_checked_chain_head: int = 0

    async def schedule(self, app: Application):
        if app.job_queue is None:
            raise RuntimeError("Application job queue is not configured")

        interval_seconds = 60 * ALERT_INTERVAL_MINUTES
        app.job_queue.run_repeating(
            self._metrics.wrap("block_processing_check", self.callback_block_processing_check),
            name="block_processing_check",
            interval=interval_seconds,
            first=0,
        )
        app.job_queue.run_daily(
            self._metrics.wrap("deposit_digest", self._flush_deposit_digest),
            name=DEPOSIT_DIGEST_JOB_NAME,
            time=self._config.deposit_digest_time,
        )
        latest_due = latest_scheduled_for(
            self._now(),
            self._config.deposit_digest_time,
        )
        storage = BotStorage(app.bot_data)
        completed_for = storage.scheduled_jobs.completed_for(DEPOSIT_DIGEST_JOB_NAME)
        pending_digest = storage.digests.events(DigestGroups.DEPOSITED_SIGNING_KEYS)
        if completed_for is None and not pending_digest:
            storage.scheduled_jobs.mark_completed(
                DEPOSIT_DIGEST_JOB_NAME,
                latest_due,
            )
        elif completed_for is None or completed_for < latest_due:
            app.job_queue.run_once(
                self._metrics.wrap("deposit_digest", self._flush_deposit_digest),
                name="deposit_digest_recovery",
                when=0,
            )
        app.job_queue.run_repeating(
            self._metrics.wrap("chain_head_poll", self._poll_chain_head),
            name="chain_head_poll",
            interval=CHAIN_HEAD_POLL_INTERVAL_SECONDS,
            first=0,
        )
        if self._secret_bundle is not None:
            app.job_queue.run_repeating(
                self._metrics.wrap("secret_rotation_check", self._check_secret_rotation),
                name="secret_rotation_check",
                interval=SECRET_WATCH_INTERVAL_SECONDS,
                first=SECRET_WATCH_INTERVAL_SECONDS,
            )

    async def _flush_deposit_digest(self, context: "BotContext") -> bool:
        async with self._deposit_digest_lock:
            scheduled_for = latest_scheduled_for(
                self._now(),
                self._config.deposit_digest_time,
            )
            store = context.bot_storage.scheduled_jobs
            completed_for = store.completed_for(DEPOSIT_DIGEST_JOB_NAME)
            if completed_for is not None and completed_for >= scheduled_for:
                return True

            through_block = context.bot_storage.block.value
            notification_count = await context.runtime.module_supervisor.flush_digest(
                DigestGroups.DEPOSITED_SIGNING_KEYS,
                through_block,
            )
            store.mark_completed(DEPOSIT_DIGEST_JOB_NAME, scheduled_for)
            logger.info(
                "Deposit digest job completed",
                extra={
                    "notification_count": notification_count,
                    "scheduled_for": scheduled_for.isoformat(),
                    "through_block": through_block,
                },
            )
        return True

    async def _check_secret_rotation(self, context: "BotContext") -> bool:
        bundle = self._secret_bundle
        if bundle is None:
            return True
        try:
            version = read_secret_version(bundle.path)
        except (OSError, RuntimeError, ValueError):
            logger.warning("Failed to read updated secret bundle", exc_info=True)
            return False
        if version == bundle.version:
            return True

        logger.info(
            "Secret bundle version changed from %s to %s; restarting",
            bundle.version,
            version,
        )
        if context.job is not None:
            context.job.schedule_removal()
        supervisor = context.runtime.module_supervisor
        supervisor.request_shutdown()
        await supervisor.raw_subscription.stop()
        return True

    async def _poll_chain_head(self, context: "BotContext"):
        try:
            self._chain_head = await self._get_block_number()
            if context is not None:
                context.runtime.health.mark_progress()
            logger.debug("Polled chain head: %s", self._chain_head)
            return True
        except Exception as exc:
            logger.warning("Failed to poll chain head: %s", exc)
            return False

    async def callback_block_processing_check(self, context: "BotContext"):
        if not self._chain_head:
            return
        if not self._last_checked_chain_head:
            self._last_checked_chain_head = self._chain_head
            return
        if self._chain_head <= self._last_checked_chain_head:
            logger.warning(
                "No new chain heads in the last %s minutes. Latest chain head: %s",
                ALERT_INTERVAL_MINUTES,
                self._chain_head,
            )
            if self._alerted:
                return
            await self._notify_admins(context, self._chain_head)
            self._alerted = True
            return
        self._last_checked_chain_head = self._chain_head
        self._alerted = False

    async def _notify_admins(self, context: "BotContext", current_block: int) -> None:
        admin_ids = context.runtime.config.admin_ids
        if not admin_ids:
            return
        message = context.runtime.module_adapter.texts.NO_NEW_BLOCKS_ADMIN_ALERT.format(
            minutes=ALERT_INTERVAL_MINUTES,
            block=current_block,
        )
        for admin_id in admin_ids:
            try:
                await context.bot.send_message(chat_id=admin_id, text=message)
            except Exception as exc:  # pragma: no cover - depends on Telegram runtime
                logger.error("Failed to notify admin %s about stalled blocks: %s", admin_id, exc)


def latest_scheduled_for(now: datetime, scheduled_time: time) -> datetime:
    if now.tzinfo is None or scheduled_time.tzinfo is None:
        raise ValueError("scheduled job calculations require timezone-aware values")
    now = now.astimezone(UTC)
    candidate = datetime.combine(now.date(), scheduled_time).astimezone(UTC)
    if candidate > now:
        candidate -= timedelta(days=1)
    return candidate
