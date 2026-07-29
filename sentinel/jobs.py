import logging
from typing import TYPE_CHECKING, Protocol

from telegram.ext import Application

from sentinel.metrics.jobs import NOOP_JOB_MIDDLEWARE, JobMetricsMiddleware

logger = logging.getLogger(__name__)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

if TYPE_CHECKING:
    from sentinel.app.context import BotContext


class ChainHeadReader(Protocol):
    async def get_block_number(self) -> int: ...


CHAIN_HEAD_POLL_INTERVAL_SECONDS = 5 * 60  # 5 minutes
ALERT_INTERVAL_MINUTES = 30


class JobContext:
    _alerted: bool = False

    def __init__(
        self,
        subscription: ChainHeadReader,
        metrics: JobMetricsMiddleware = NOOP_JOB_MIDDLEWARE,
    ) -> None:
        self._subscription = subscription
        self._metrics = metrics
        self._chain_head: int = 0
        self._last_checked_chain_head: int = 0

    async def schedule(self, app: Application):
        if app.job_queue is None:
            raise RuntimeError("Application job queue is not configured")

        interval_seconds = 60 * ALERT_INTERVAL_MINUTES
        app.job_queue.run_repeating(
            self._metrics.wrap("block_processing_check", self.callback_block_processing_check),
            interval=interval_seconds,
            first=0,
        )
        app.job_queue.run_repeating(
            self._metrics.wrap("chain_head_poll", self._poll_chain_head),
            interval=CHAIN_HEAD_POLL_INTERVAL_SECONDS,
            first=0,
        )

    async def _poll_chain_head(self, context: "BotContext"):
        try:
            self._chain_head = await self._subscription.get_block_number()
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
