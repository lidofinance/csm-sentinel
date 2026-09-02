import logging
from typing import Iterable, TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, TelegramError

from sentinel.handlers.admin.common import admin_only
from sentinel.handlers.state import Callback, States
from sentinel.handlers.utils import resolve_target_chats_for_node_operators

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sentinel.app.context import BotContext


def _texts(context: "BotContext"):
    return context.runtime.module_adapter.texts


class BroadcastSession:
    PROMPT_CHAT_ID_KEY = "broadcast_prompt_chat_id"
    PROMPT_MESSAGE_ID_KEY = "broadcast_prompt_message_id"
    SELECTED_IDS_KEY = "broadcast_selected"
    PREVIEW_CHAT_ID_KEY = "broadcast_preview_chat_id"
    PREVIEW_MESSAGE_ID_KEY = "broadcast_preview_message_id"

    def __init__(self, context: "BotContext") -> None:
        self._context = context

    def store_prompt(self, message: Message | bool | None) -> None:
        if not isinstance(message, Message):
            return
        self._context.user_data[self.PROMPT_CHAT_ID_KEY] = message.chat_id
        self._context.user_data[self.PROMPT_MESSAGE_ID_KEY] = message.message_id

    def clear_prompt(self) -> None:
        self._context.user_data.pop(self.PROMPT_CHAT_ID_KEY, None)
        self._context.user_data.pop(self.PROMPT_MESSAGE_ID_KEY, None)

    def set_selected_ids(self, ids: set[str]) -> None:
        self._context.user_data[self.SELECTED_IDS_KEY] = ids

    def get_selected_ids(self) -> set[str] | None:
        selected = self._context.user_data.get(self.SELECTED_IDS_KEY)
        return selected if isinstance(selected, set) else None

    def clear_selected_ids(self) -> None:
        self._context.user_data.pop(self.SELECTED_IDS_KEY, None)

    def get_prompt_chat_id(self) -> int | None:
        return self._context.user_data.get(self.PROMPT_CHAT_ID_KEY)

    def get_prompt_message_id(self) -> int | None:
        return self._context.user_data.get(self.PROMPT_MESSAGE_ID_KEY)

    def store_preview(self, chat_id: int, message_id: int) -> None:
        self._context.user_data[self.PREVIEW_CHAT_ID_KEY] = chat_id
        self._context.user_data[self.PREVIEW_MESSAGE_ID_KEY] = message_id

    def clear_preview(self) -> None:
        self._context.user_data.pop(self.PREVIEW_CHAT_ID_KEY, None)
        self._context.user_data.pop(self.PREVIEW_MESSAGE_ID_KEY, None)

    def get_preview_chat_id(self) -> int | None:
        return self._context.user_data.get(self.PREVIEW_CHAT_ID_KEY)

    def get_preview_message_id(self) -> int | None:
        return self._context.user_data.get(self.PREVIEW_MESSAGE_ID_KEY)

    def consume_preview(
        self,
        *,
        prompt_chat_id: int,
        prompt_message_id: int,
        preview_message_id: int,
    ) -> tuple[int, int] | None:
        if (
            self.get_prompt_chat_id() != prompt_chat_id
            or self.get_prompt_message_id() != prompt_message_id
            or self.get_preview_message_id() != preview_message_id
        ):
            return None
        preview_chat_id = self.get_preview_chat_id()
        if preview_chat_id is None:
            return None
        self.clear_preview()
        return preview_chat_id, preview_message_id


@admin_only(failure_state=States.WELCOME)
async def broadcast_menu(update: Update, context: "BotContext") -> States:
    query = update.callback_query
    if query is None:
        return States.ADMIN_BROADCAST
    await query.answer()
    await _delete_preview_message(context)
    session = BroadcastSession(context)
    keyboard = [
        [
            InlineKeyboardButton(
                _texts(context).ADMIN_BROADCAST_ALL,
                callback_data=Callback.ADMIN_BROADCAST_ALL.value,
            )
        ],
        [
            InlineKeyboardButton(
                _texts(context).ADMIN_BROADCAST_BY_NO,
                callback_data=Callback.ADMIN_BROADCAST_BY_NO.value,
            )
        ],
        [InlineKeyboardButton(_texts(context).BUTTON_BACK, callback_data=Callback.BACK.value)],
    ]
    updated_message = await query.edit_message_text(
        text=_texts(context).ADMIN_BROADCAST_MENU_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    session.store_prompt(updated_message)
    session.clear_selected_ids()
    return States.ADMIN_BROADCAST


@admin_only(failure_state=States.WELCOME)
async def broadcast_all_prompt(update: Update, context: "BotContext") -> States:
    query = update.callback_query
    if query is None:
        return States.ADMIN_BROADCAST_MESSAGE_ALL
    await query.answer()
    await _delete_preview_message(context)
    session = BroadcastSession(context)
    keyboard = [
        [InlineKeyboardButton(_texts(context).BUTTON_BACK, callback_data=Callback.BACK.value)]
    ]
    updated_message = await query.edit_message_text(
        text=_texts(context).ADMIN_BROADCAST_ENTER_MESSAGE_ALL,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    session.store_prompt(updated_message)
    return States.ADMIN_BROADCAST_MESSAGE_ALL


@admin_only(failure_state=States.WELCOME)
async def broadcast_by_no(update: Update, context: "BotContext") -> States:
    query = update.callback_query
    if query is None:
        return States.ADMIN_BROADCAST_SELECT_NO
    await query.answer()
    await _delete_preview_message(context)
    session = BroadcastSession(context)
    session.clear_selected_ids()
    keyboard = [
        [InlineKeyboardButton(_texts(context).BUTTON_BACK, callback_data=Callback.BACK.value)]
    ]
    updated_message = await query.edit_message_text(
        text=_texts(context).ADMIN_BROADCAST_ENTER_NO_IDS,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    session.store_prompt(updated_message)
    return States.ADMIN_BROADCAST_SELECT_NO


@admin_only(failure_state=States.ADMIN_BROADCAST_MESSAGE_ALL)
async def broadcast_all_message(update: Update, context: "BotContext") -> States:
    message = update.message
    chat_id = _resolve_chat_id(message, update)
    if chat_id is None:
        return States.ADMIN_BROADCAST_MESSAGE_ALL
    session = BroadcastSession(context)
    await _delete_preview_message(context)
    preview_id = await _copy_preview_message(context, chat_id, message)
    await _delete_user_message(message)
    if preview_id is None:
        await _edit_broadcast_prompt_message(
            context,
            chat_id,
            "Unable to preview that message. Please try again.",
            _back_markup(context),
        )
        return States.ADMIN_BROADCAST_MESSAGE_ALL

    await _edit_broadcast_prompt_message(
        context,
        chat_id,
        _format_preview_text(context, _texts(context).ADMIN_BROADCAST_PREVIEW_ALL),
        _confirmation_markup(
            context,
            Callback.ADMIN_BROADCAST_CONFIRM_ALL,
            preview_id,
        ),
    )
    session.store_preview(chat_id, preview_id)
    return States.ADMIN_BROADCAST_MESSAGE_ALL


@admin_only(failure_state=States.ADMIN_BROADCAST_SELECT_NO)
async def broadcast_enter_no_ids_message(update: Update, context: "BotContext") -> States:
    message = update.message
    chat_id = _resolve_chat_id(message, update)
    if chat_id is None:
        return States.ADMIN_BROADCAST_SELECT_NO
    session = BroadcastSession(context)

    raw_input = (message.text or "").strip() if message else ""
    await _delete_user_message(message)

    if not raw_input:
        await _edit_broadcast_prompt_message(
            context,
            chat_id,
            f"{_texts(context).ADMIN_BROADCAST_NO_IDS_INVALID}\n\n{_texts(context).ADMIN_BROADCAST_ENTER_NO_IDS}",
            _back_markup(context),
        )
        return States.ADMIN_BROADCAST_SELECT_NO

    raw = raw_input
    raw = raw.replace("#", "").replace(" ", ",")
    ids: set[str] = set()
    for token in filter(None, (t.strip() for t in raw.split(","))):
        if token.isdigit():
            ids.add(token)
    if not ids:
        await _edit_broadcast_prompt_message(
            context,
            chat_id,
            f"{_texts(context).ADMIN_BROADCAST_NO_IDS_INVALID}\n\n{_texts(context).ADMIN_BROADCAST_ENTER_NO_IDS}",
            _back_markup(context),
        )
        return States.ADMIN_BROADCAST_SELECT_NO
    session.set_selected_ids(ids)
    pretty_ids = ", ".join(sorted(f"#{i}" for i in ids))
    prompt_text = (
        f"Node operators selected: {pretty_ids}\n\n"
        "Please enter the message to broadcast to these node operators:"
    )
    await _edit_broadcast_prompt_message(
        context,
        chat_id,
        prompt_text,
        _back_markup(context),
    )
    return States.ADMIN_BROADCAST_MESSAGE_SELECTED


@admin_only(failure_state=States.ADMIN_BROADCAST_MESSAGE_SELECTED)
async def broadcast_selected_message(update: Update, context: "BotContext") -> States:
    message = update.message
    chat_id = _resolve_chat_id(message, update)
    if chat_id is None:
        return States.ADMIN_BROADCAST_MESSAGE_SELECTED
    session = BroadcastSession(context)
    selected = session.get_selected_ids()
    if not selected:
        await _delete_user_message(message)
        await _edit_broadcast_prompt_message(
            context,
            chat_id,
            "No node operators selected. Please provide node operator IDs first.",
            _back_markup(context),
        )
        return States.ADMIN_BROADCAST_SELECT_NO
    await _delete_preview_message(context)
    preview_id = await _copy_preview_message(context, chat_id, message)
    await _delete_user_message(message)
    if preview_id is None:
        await _edit_broadcast_prompt_message(
            context,
            chat_id,
            "Unable to preview that message. Please try again.",
            _back_markup(context),
        )
        return States.ADMIN_BROADCAST_MESSAGE_SELECTED

    pretty_ids = ", ".join(sorted(f"#{i}" for i in selected))
    header = _texts(context).ADMIN_BROADCAST_PREVIEW_SELECTED.format(targets=pretty_ids)
    await _edit_broadcast_prompt_message(
        context,
        chat_id,
        _format_preview_text(context, header),
        _confirmation_markup(
            context,
            Callback.ADMIN_BROADCAST_CONFIRM_SELECTED,
            preview_id,
        ),
    )
    session.store_preview(chat_id, preview_id)
    return States.ADMIN_BROADCAST_MESSAGE_SELECTED


@admin_only(failure_state=States.ADMIN_BROADCAST_MESSAGE_ALL)
async def broadcast_all_confirm(update: Update, context: "BotContext") -> States:
    query = update.callback_query
    if query is None:
        return States.ADMIN_BROADCAST_MESSAGE_ALL
    confirmation = _consume_confirmation(
        update,
        context,
        callback=Callback.ADMIN_BROADCAST_CONFIRM_ALL,
    )
    if confirmation is None:
        await query.answer("This confirmation has expired.", show_alert=True)
        return States.ADMIN_BROADCAST_MESSAGE_ALL

    session = BroadcastSession(context)
    await query.answer()
    preview_chat_id, preview_message_id = confirmation

    bot_storage = context.bot_storage
    targets = bot_storage.resolve_target_chats(bot_storage.node_operator_chats.ids())
    if not targets:
        await _delete_preview_by_reference(
            context,
            preview_chat_id,
            preview_message_id,
        )
        updated = await query.edit_message_text(
            text="No subscribers to notify.",
            reply_markup=_back_markup(context),
        )
        session.store_prompt(updated)
        return States.ADMIN_BROADCAST_MESSAGE_ALL

    sent, failed = await _broadcast_copy_to_chats(
        context,
        targets,
        preview_chat_id,
        preview_message_id,
    )
    logger.info("Admin broadcast (all) attempted: sent=%s failed=%s", sent, failed)
    await _delete_preview_by_reference(
        context,
        preview_chat_id,
        preview_message_id,
    )
    result_text = f"Broadcast sent to {sent} chat(s). Failures: {failed}."
    updated = await query.edit_message_text(text=result_text, reply_markup=_back_markup(context))
    session.store_prompt(updated)
    return States.ADMIN_BROADCAST_MESSAGE_ALL


@admin_only(failure_state=States.ADMIN_BROADCAST_MESSAGE_SELECTED)
async def broadcast_selected_confirm(update: Update, context: "BotContext") -> States:
    query = update.callback_query
    if query is None:
        return States.ADMIN_BROADCAST_MESSAGE_SELECTED
    session = BroadcastSession(context)
    selected = session.get_selected_ids()
    if not selected:
        await query.answer("This confirmation has expired.", show_alert=True)
        return States.ADMIN_BROADCAST_MESSAGE_SELECTED

    confirmation = _consume_confirmation(
        update,
        context,
        callback=Callback.ADMIN_BROADCAST_CONFIRM_SELECTED,
    )
    if confirmation is None:
        await query.answer("This confirmation has expired.", show_alert=True)
        return States.ADMIN_BROADCAST_MESSAGE_SELECTED

    await query.answer()
    preview_chat_id, preview_message_id = confirmation
    selected_snapshot = frozenset(selected)
    bot_storage = context.bot_storage
    targets = resolve_target_chats_for_node_operators(bot_storage, selected_snapshot)
    if not targets:
        await _delete_preview_by_reference(
            context,
            preview_chat_id,
            preview_message_id,
        )
        updated = await query.edit_message_text(
            text="No active subscribers for the selected node operators.",
            reply_markup=_back_markup(context),
        )
        session.store_prompt(updated)
        return States.ADMIN_BROADCAST_MESSAGE_SELECTED

    sent, failed = await _broadcast_copy_to_chats(
        context,
        targets,
        preview_chat_id,
        preview_message_id,
    )
    pretty_ids = ", ".join(sorted(f"#{i}" for i in selected_snapshot))
    logger.info(
        "Admin broadcast (selected) attempted: node_operators=%s sent=%s failed=%s",
        pretty_ids,
        sent,
        failed,
    )
    session.clear_selected_ids()
    await _delete_preview_by_reference(
        context,
        preview_chat_id,
        preview_message_id,
    )
    result_text = f"Broadcast to {pretty_ids}: sent to {sent} chat(s). Failures: {failed}."
    updated = await query.edit_message_text(text=result_text, reply_markup=_back_markup(context))
    session.store_prompt(updated)
    return States.ADMIN_BROADCAST_MESSAGE_SELECTED


async def _broadcast_copy_to_chats(
    context: "BotContext",
    chats: Iterable[int],
    preview_chat_id: int,
    preview_message_id: int,
) -> tuple[int, int]:
    sent, failed = 0, 0
    for chat_id in chats:
        try:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=preview_chat_id,
                message_id=preview_message_id,
            )
            sent += 1
        except Exception as exc:  # pragma: no cover - depends on Telegram runtime
            logger.error("Broadcast error to %s: %s", chat_id, exc)
            failed += 1
    return sent, failed


async def _edit_broadcast_prompt_message(
    context: "BotContext",
    chat_id: int | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    session = BroadcastSession(context)
    target_chat_id = chat_id or session.get_prompt_chat_id()
    if target_chat_id is None:
        logger.warning("Cannot edit broadcast prompt without a chat id")
        return
    message_id = session.get_prompt_message_id()
    if not message_id:
        sent = await context.bot.send_message(
            chat_id=target_chat_id, text=text, reply_markup=reply_markup
        )
        session.store_prompt(sent)
        return
    try:
        updated = await context.bot.edit_message_text(
            chat_id=target_chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
        session.store_prompt(updated)
    except BadRequest as exc:
        logger.debug("Failed to edit broadcast prompt message: %s", exc)
        sent = await context.bot.send_message(
            chat_id=target_chat_id, text=text, reply_markup=reply_markup
        )
        session.store_prompt(sent)


async def _delete_user_message(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except TelegramError as exc:
        logger.debug("Failed to delete admin broadcast input: %s", exc)


def _back_markup(context: "BotContext") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(_texts(context).BUTTON_BACK, callback_data=Callback.BACK.value)]]
    )


def _confirmation_markup(
    context: "BotContext",
    callback: Callback,
    preview_message_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _texts(context).BUTTON_SEND_BROADCAST,
                    callback_data=f"{callback.value}:{preview_message_id}",
                )
            ],
            [InlineKeyboardButton(_texts(context).BUTTON_BACK, callback_data=Callback.BACK.value)],
        ]
    )


def _format_preview_text(context: "BotContext", header: str) -> str:
    return f"{header}\n\n{_texts(context).ADMIN_BROADCAST_CONFIRM_HINT}\n\nPreview is shown below."


async def _copy_preview_message(
    context: "BotContext",
    chat_id: int,
    message: Message | None,
) -> int | None:
    if message is None:
        return None
    try:
        preview = await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        return preview.message_id
    except TelegramError as exc:
        logger.warning("Failed to copy broadcast preview message: %s", exc)
        return None


def _consume_confirmation(
    update: Update,
    context: "BotContext",
    *,
    callback: Callback,
) -> tuple[int, int] | None:
    query = update.callback_query
    message = query.message if query is not None else None
    preview_message_id = _confirmation_preview_message_id(
        query.data if query is not None else None,
        callback,
    )
    if message is None or preview_message_id is None:
        return None
    return BroadcastSession(context).consume_preview(
        prompt_chat_id=message.chat.id,
        prompt_message_id=message.message_id,
        preview_message_id=preview_message_id,
    )


def _confirmation_preview_message_id(data: str | None, callback: Callback) -> int | None:
    prefix = f"{callback.value}:"
    if data is None or not data.startswith(prefix):
        return None
    raw_message_id = data[len(prefix) :]
    return int(raw_message_id) if raw_message_id.isdecimal() else None


async def _delete_preview_message(context: "BotContext") -> None:
    session = BroadcastSession(context)
    preview_chat_id = session.get_preview_chat_id()
    preview_message_id = session.get_preview_message_id()
    session.clear_preview()
    if preview_chat_id is None or preview_message_id is None:
        return
    await _delete_preview_by_reference(context, preview_chat_id, preview_message_id)


async def _delete_preview_by_reference(
    context: "BotContext",
    preview_chat_id: int,
    preview_message_id: int,
) -> None:
    try:
        await context.bot.delete_message(chat_id=preview_chat_id, message_id=preview_message_id)
    except TelegramError as exc:
        logger.debug("Failed to delete broadcast preview message: %s", exc)


def _resolve_chat_id(message: Message | None, update: Update) -> int | None:
    if message is not None:
        return message.chat_id
    chat = update.effective_chat
    return chat.id if chat is not None else None
