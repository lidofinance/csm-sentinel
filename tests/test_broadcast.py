from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from sentinel.handlers.admin import broadcast as broadcast_handlers
from sentinel.handlers.admin.broadcast import BroadcastSession
from sentinel.handlers.state import Callback, States


PROMPT_CHAT_ID = 100
PROMPT_MESSAGE_ID = 200


def _callback_update(preview_message_id: int):
    query = SimpleNamespace(
        data=f"11:{preview_message_id}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=PROMPT_CHAT_ID),
            message_id=PROMPT_MESSAGE_ID,
        ),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(return_value=None),
    )
    return SimpleNamespace(callback_query=query)


def _context():
    texts = SimpleNamespace(
        BUTTON_BACK="Back",
        BUTTON_SEND_BROADCAST="Send broadcast",
    )
    return SimpleNamespace(
        runtime=SimpleNamespace(module_adapter=SimpleNamespace(texts=texts)),
        user_data={
            BroadcastSession.PROMPT_CHAT_ID_KEY: PROMPT_CHAT_ID,
            BroadcastSession.PROMPT_MESSAGE_ID_KEY: PROMPT_MESSAGE_ID,
            BroadcastSession.PREVIEW_CHAT_ID_KEY: PROMPT_CHAT_ID,
            BroadcastSession.PREVIEW_MESSAGE_ID_KEY: 301,
            BroadcastSession.SELECTED_IDS_KEY: {"99"},
            BroadcastSession.MESSAGE_TEXT_KEY: "current preview text",
        },
        bot=SimpleNamespace(
            copy_message=AsyncMock(),
            delete_message=AsyncMock(),
        ),
        bot_storage=object(),
    )


def test_broadcast_session_preserves_current_preview_after_stale_consume():
    context = _context()
    session = BroadcastSession(context)
    original_user_data = dict(context.user_data)

    stale = session.consume_preview(
        prompt_chat_id=PROMPT_CHAT_ID,
        prompt_message_id=PROMPT_MESSAGE_ID,
        preview_message_id=300,
    )
    wrong_prompt = session.consume_preview(
        prompt_chat_id=PROMPT_CHAT_ID,
        prompt_message_id=PROMPT_MESSAGE_ID + 1,
        preview_message_id=301,
    )

    assert stale is None
    assert wrong_prompt is None
    assert context.user_data == original_user_data

    consumed = session.consume_preview(
        prompt_chat_id=PROMPT_CHAT_ID,
        prompt_message_id=PROMPT_MESSAGE_ID,
        preview_message_id=301,
    )

    assert consumed == (PROMPT_CHAT_ID, 301)
    assert BroadcastSession.PREVIEW_CHAT_ID_KEY not in context.user_data
    assert BroadcastSession.PREVIEW_MESSAGE_ID_KEY not in context.user_data


def test_confirmation_button_carries_preview_message_id():
    context = _context()

    markup = broadcast_handlers._confirmation_markup(
        context,
        Callback.ADMIN_BROADCAST_CONFIRM_SELECTED,
        301,
    )

    assert markup.inline_keyboard[0][0].callback_data == "11:301"


@pytest.mark.asyncio
async def test_stale_selected_confirmation_does_not_send_or_mutate_current_session():
    context = _context()
    update = _callback_update(300)
    original_user_data = dict(context.user_data)

    state = await broadcast_handlers.broadcast_selected_confirm.__wrapped__(update, context)

    assert state == States.ADMIN_BROADCAST_MESSAGE_SELECTED
    update.callback_query.answer.assert_awaited_once_with(
        "This confirmation has expired.",
        show_alert=True,
    )
    context.bot.copy_message.assert_not_awaited()
    assert context.user_data == original_user_data


@pytest.mark.asyncio
async def test_selected_confirmation_consumes_preview_before_sending(monkeypatch):
    context = _context()
    update = _callback_update(301)
    resolve_targets = Mock(return_value={500, 501})
    monkeypatch.setattr(
        broadcast_handlers,
        "resolve_target_chats_for_node_operators",
        resolve_targets,
    )

    state = await broadcast_handlers.broadcast_selected_confirm.__wrapped__(update, context)
    repeated_state = await broadcast_handlers.broadcast_selected_confirm.__wrapped__(
        update,
        context,
    )

    assert state == States.ADMIN_BROADCAST_MESSAGE_SELECTED
    assert repeated_state == States.ADMIN_BROADCAST_MESSAGE_SELECTED
    resolve_targets.assert_called_once_with(context.bot_storage, frozenset({"99"}))
    assert context.bot.copy_message.await_args_list == [
        call(chat_id=500, from_chat_id=PROMPT_CHAT_ID, message_id=301),
        call(chat_id=501, from_chat_id=PROMPT_CHAT_ID, message_id=301),
    ]
    context.bot.delete_message.assert_awaited_once_with(
        chat_id=PROMPT_CHAT_ID,
        message_id=301,
    )
    assert BroadcastSession.PREVIEW_CHAT_ID_KEY not in context.user_data
    assert BroadcastSession.PREVIEW_MESSAGE_ID_KEY not in context.user_data
    assert BroadcastSession.SELECTED_IDS_KEY not in context.user_data
    assert BroadcastSession.MESSAGE_TEXT_KEY not in context.user_data
    assert update.callback_query.answer.await_args_list == [
        call(),
        call("This confirmation has expired.", show_alert=True),
    ]


@pytest.mark.asyncio
async def test_all_confirmation_is_bound_to_its_preview():
    context = _context()
    context.user_data[BroadcastSession.PREVIEW_MESSAGE_ID_KEY] = 401
    context.bot_storage = SimpleNamespace(
        node_operator_chats=SimpleNamespace(ids=Mock(return_value={"1", "2"})),
        resolve_target_chats=Mock(return_value={600}),
    )
    update = _callback_update(401)
    update.callback_query.data = "10:401"

    state = await broadcast_handlers.broadcast_all_confirm.__wrapped__(update, context)

    assert state == States.ADMIN_BROADCAST_MESSAGE_ALL
    context.bot_storage.resolve_target_chats.assert_called_once_with({"1", "2"})
    context.bot.copy_message.assert_awaited_once_with(
        chat_id=600,
        from_chat_id=PROMPT_CHAT_ID,
        message_id=401,
    )
