import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from telegram import LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.ext import Application, TypeHandler

from sentinel.app.storage import BotStorage
from sentinel.models import EventNotification

if TYPE_CHECKING:
    from sentinel.app.context import BotContext
    from sentinel.notifications import EventMessageEngine

logger = logging.getLogger(__name__)


class TelegramProcessingStateProvider:
    def __init__(self, application: Application) -> None:
        self._application = application

    @property
    def state(self) -> BotStorage:
        return BotStorage(self._application.bot_data)


class TelegramNotificationSink:
    def __init__(self, application: Application) -> None:
        self._application = application

    async def emit(self, notification: EventNotification) -> None:
        await self._application.update_queue.put(notification)


class TelegramNotificationHandler:
    """Deliver EventNotification updates to Telegram chats."""

    def __init__(
        self,
        application: Application,
        event_messages_provider: Callable[[], "EventMessageEngine"],
    ) -> None:
        self.application = application
        self._event_messages_provider = event_messages_provider

    async def handle_event_log(self, event: EventNotification, context: "BotContext"):
        bot_storage = context.bot_storage
        actual_chat_ids = bot_storage.actual_chat_ids()
        node_operator_chats = bot_storage.node_operator_chats
        plan = await self._event_messages_provider().get_notification_plan(event)
        if plan is None:
            return

        sent_messages = 0
        targeted_chats: set[int] = set()

        for node_operator_id, message in plan.per_node_operator.items():
            chats = node_operator_chats.chats_for(node_operator_id)
            chats = chats.intersection(actual_chat_ids)
            if not chats:
                continue
            for chat in chats:
                try:
                    await context.bot.send_message(
                        chat_id=chat,
                        text=message,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                    targeted_chats.add(chat)
                    sent_messages += 1
                except Exception as exc:  # pragma: no cover - depends on Telegram runtime
                    logger.error("Error sending message to chat %s: %s", chat, exc)

        if plan.broadcast:
            targeted_ids = set(plan.per_node_operator.keys())
            if plan.broadcast_node_operator_ids is not None:
                candidate_ids = set(plan.broadcast_node_operator_ids)
                candidate_ids -= targeted_ids

                broadcast_chats: set[int] = set()
                for node_operator_id in candidate_ids:
                    broadcast_chats.update(node_operator_chats.chats_for(node_operator_id))
            else:
                broadcast_chats = set(actual_chat_ids)

            broadcast_chats = broadcast_chats.intersection(actual_chat_ids)
            broadcast_chats -= targeted_chats

            for chat in broadcast_chats:
                try:
                    await context.bot.send_message(
                        chat_id=chat,
                        text=plan.broadcast,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                    sent_messages += 1
                except Exception as exc:  # pragma: no cover - depends on Telegram runtime
                    logger.error("Error sending message to chat %s: %s", chat, exc)

        if sent_messages:
            logger.info(
                "Notification handled on the block %s: %s; Messages sent: %s",
                event.block,
                event.readable(),
                sent_messages,
                extra={
                    "event_name": event.event,
                    "block": event.block,
                    "source_event_count": len(event.source_events),
                    "sent_messages": sent_messages,
                },
            )

    def register_handlers(self) -> None:
        """Attach type handlers for event updates to the application."""
        self.application.add_handler(
            TypeHandler(EventNotification, self.handle_event_log, block=False)
        )
