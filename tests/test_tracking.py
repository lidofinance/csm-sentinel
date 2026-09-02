from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telegram import Update
from telegram.ext import ApplicationBuilder

from sentinel.app.application import SentinelApplication
from sentinel.app.context import BotContext
from sentinel.app.storage import BotStorage, ChatStorage, create_persistence
from sentinel.handlers.tracking import chat_migration

OLD_CHAT_ID = -100
NEW_CHAT_ID = -100_200


def _build_application(storage_path: Path | None = None):
    builder = ApplicationBuilder().application_class(SentinelApplication).token("123:TEST")
    if storage_path is not None:
        builder.persistence(create_persistence(storage_path))
    return builder.build()


def _migration_message(kind: str):
    if kind == "to":
        return SimpleNamespace(
            chat_id=OLD_CHAT_ID,
            chat=SimpleNamespace(id=OLD_CHAT_ID),
            migrate_to_chat_id=NEW_CHAT_ID,
            migrate_from_chat_id=None,
        )
    return SimpleNamespace(
        chat_id=NEW_CHAT_ID,
        chat=SimpleNamespace(id=NEW_CHAT_ID),
        migrate_to_chat_id=None,
        migrate_from_chat_id=OLD_CHAT_ID,
    )


def _build_context(application):
    bot_storage = BotStorage(application.bot_data)
    context = SimpleNamespace(
        application=application,
        bot_storage=bot_storage,
        chat_storage=lambda chat_data: ChatStorage(chat_data),
    )
    return cast(BotContext, context), bot_storage


async def _apply_migrations(context: BotContext, sequence: tuple[str, ...]) -> None:
    for kind in sequence:
        update = cast(Update, SimpleNamespace(message=_migration_message(kind)))
        await chat_migration(update, context)


def _per_chat_operator_ids(application) -> set[str]:
    return set(application.chat_data[NEW_CHAT_ID].get("node_operators", set()))


def _mapped_operator_ids(bot_storage: BotStorage) -> set[str]:
    return {
        node_operator_id
        for node_operator_id in bot_storage.node_operator_chats.ids()
        if NEW_CHAT_ID in bot_storage.node_operator_chats.chats_for(node_operator_id)
    }


@pytest.mark.parametrize(
    "sequence",
    [
        ("to", "from"),
        ("from", "to"),
        ("to", "to"),
        ("from", "from"),
    ],
)
async def test_chat_migration_is_idempotent_for_message_order_and_replay(sequence):
    application = _build_application()
    application.chat_data[OLD_CHAT_ID]["node_operators"] = {"42"}
    application.bot_data.update(
        {
            "group_ids": {OLD_CHAT_ID},
            "no_ids_to_chats": {"42": {OLD_CHAT_ID}},
        }
    )
    context, bot_storage = _build_context(application)

    await _apply_migrations(context, sequence)

    assert _per_chat_operator_ids(application) == {"42"}
    assert _mapped_operator_ids(bot_storage) == {"42"}
    assert bot_storage.groups.all() == {NEW_CHAT_ID}

    chat_storage = ChatStorage(application.chat_data[NEW_CHAT_ID])
    assert chat_storage.node_operators.unfollow("42") is True
    bot_storage.node_operator_chats.unsubscribe("42", NEW_CHAT_ID)
    assert chat_storage.node_operators.ids() == set()
    assert _mapped_operator_ids(bot_storage) == set()


async def test_chat_migration_merges_preexisting_new_chat_subscriptions():
    application = _build_application()
    application.chat_data[OLD_CHAT_ID]["node_operators"] = {"42"}
    application.chat_data[NEW_CHAT_ID]["node_operators"] = {"7"}
    application.bot_data.update(
        {
            "group_ids": {OLD_CHAT_ID, NEW_CHAT_ID},
            "no_ids_to_chats": {
                "42": {OLD_CHAT_ID},
                "7": {NEW_CHAT_ID},
            },
        }
    )
    context, bot_storage = _build_context(application)

    await _apply_migrations(context, ("to",))

    assert _per_chat_operator_ids(application) == {"7", "42"}
    assert _mapped_operator_ids(bot_storage) == {"7", "42"}
    assert OLD_CHAT_ID not in application.chat_data


async def test_chat_migration_parity_survives_persistence_restart(tmp_path):
    application = _build_application(tmp_path)
    application.chat_data[OLD_CHAT_ID]["node_operators"] = {"42"}
    application.bot_data.update(
        {
            "group_ids": {OLD_CHAT_ID},
            "no_ids_to_chats": {"42": {OLD_CHAT_ID}},
        }
    )
    context, _ = _build_context(application)

    await _apply_migrations(context, ("to", "from"))
    await application.update_persistence()
    await application.persistence.flush()

    restored = create_persistence(tmp_path)
    restored_chat_data = await restored.get_chat_data()
    restored_bot_storage = BotStorage(await restored.get_bot_data())
    assert restored_chat_data[NEW_CHAT_ID]["node_operators"] == {"42"}
    assert _mapped_operator_ids(restored_bot_storage) == {"42"}
