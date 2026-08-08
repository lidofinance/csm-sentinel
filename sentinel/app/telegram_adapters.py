import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from telegram import LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.ext import Application, TypeHandler

from sentinel.models import EventNotification
from sentinel.notifications import (
    BroadcastDelivery,
    OperatorMessagesDelivery,
    PerChatDelivery,
)

if TYPE_CHECKING:
    from sentinel.app.context import BotContext
    from sentinel.modules.event_engine import EventMessageEngineBase

logger = logging.getLogger(__name__)


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
        event_messages_provider: Callable[[], "EventMessageEngineBase"],
    ) -> None:
        self.application = application
        self._event_messages_provider = event_messages_provider

    async def handle_event_log(self, event: EventNotification, context: "BotContext"):
        plan = await self._event_messages_provider().get_notification_plan(event)
        if plan is None:
            return

        delivery = plan.delivery
        if isinstance(delivery, BroadcastDelivery):
            sent_messages = await self._deliver_broadcast(delivery, context)
        elif isinstance(delivery, OperatorMessagesDelivery):
            sent_messages = await self._deliver_operator_messages(delivery, context)
        elif isinstance(delivery, PerChatDelivery):
            sent_messages = await self._deliver_per_chat(delivery, context)
        else:  # pragma: no cover - exhaustive runtime guard
            raise RuntimeError(f"Unsupported notification delivery: {delivery!r}")

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

    async def _deliver_broadcast(
        self,
        delivery: BroadcastDelivery,
        context: "BotContext",
    ) -> int:
        bot_storage = context.bot_storage
        actual_chat_ids = bot_storage.actual_chat_ids()
        node_operator_chats = bot_storage.node_operator_chats
        if delivery.operator_ids is None:
            chats = set(actual_chat_ids)
        else:
            chats = node_operator_chats.resolve_targets(
                delivery.operator_ids,
                actual_chat_ids,
            )
        return await self._send_to_chats(chats, delivery.message, context)

    async def _deliver_operator_messages(
        self,
        delivery: OperatorMessagesDelivery,
        context: "BotContext",
    ) -> int:
        bot_storage = context.bot_storage
        actual_chat_ids = bot_storage.actual_chat_ids()
        node_operator_chats = bot_storage.node_operator_chats
        sent_messages = 0
        for node_operator_id, message in delivery.messages.items():
            chats = node_operator_chats.chats_for(node_operator_id) & actual_chat_ids
            sent_messages += await self._send_to_chats(chats, message, context)
        return sent_messages

    async def _deliver_per_chat(
        self,
        delivery: PerChatDelivery,
        context: "BotContext",
    ) -> int:
        bot_storage = context.bot_storage
        actual_chat_ids = bot_storage.actual_chat_ids()
        node_operator_chats = bot_storage.node_operator_chats
        operator_ids_by_chat: dict[int, set[str]] = {}

        for node_operator_id in delivery.operator_ids:
            for chat_id in node_operator_chats.chats_for(node_operator_id):
                if chat_id in actual_chat_ids:
                    operator_ids_by_chat.setdefault(chat_id, set()).add(node_operator_id)

        sent_messages = 0
        for chat_id, node_operator_ids in operator_ids_by_chat.items():
            for message in delivery.render(frozenset(node_operator_ids)):
                sent_messages += await self._send_to_chats(
                    {chat_id},
                    message,
                    context,
                )
        return sent_messages

    @staticmethod
    async def _send_to_chats(chats: set[int], message: str, context: "BotContext") -> int:
        sent_messages = 0
        for chat_id in chats:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                sent_messages += 1
            except Exception as exc:  # pragma: no cover - depends on Telegram runtime
                logger.error("Error sending message to chat %s: %s", chat_id, exc)
        return sent_messages

    def register_handlers(self) -> None:
        """Attach type handlers for event updates to the application."""
        self.application.add_handler(
            TypeHandler(EventNotification, self.handle_event_log, block=False)
        )
