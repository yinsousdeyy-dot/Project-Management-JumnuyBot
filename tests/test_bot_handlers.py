"""Offline tests for the Telegram handler layer (SCRUM-45).

Every test runs without network access: Telegram objects are constructed in
memory, the bot is an :class:`unittest.mock.AsyncMock`, storage is
``Database(":memory:")`` and the summarizer has no AI client (so it never calls
the Rayu gateway). Handlers are async, but pytest-asyncio is not a dependency,
so each scenario is driven with :func:`asyncio.run`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    Update,
    User,
)
from telegram.ext import (
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
)

from src import bot_handlers
from src.bot_services import build_services
from src.config import Database, Settings

BOT_ID = 999
BOT_USERNAME = "testbot"
ADMIN_ID = 1
NON_ADMIN_ID = 42
STUDENT_ID = 500
CHAT_ID = -1001234567890
OTHER_CHAT_ID = -1009876543210


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def make_settings() -> Settings:
    """Return deterministic settings with one admin and no Rayu key."""

    return Settings(
        telegram_bot_token="123456:TEST-token",
        admin_user_ids=(ADMIN_ID,),
        bot_username=BOT_USERNAME,
        cache_ttl_seconds=60,
    )


def make_bot() -> AsyncMock:
    """Return an AsyncMock bot that knows its own id."""

    bot = AsyncMock()
    bot.id = BOT_ID
    return bot


def make_user(
    user_id: int = STUDENT_ID,
    *,
    username: str = "student",
    is_bot: bool = False,
    first_name: str = "Student",
) -> User:
    """Return a Telegram user."""

    return User(id=user_id, first_name=first_name, is_bot=is_bot, username=username)


def make_message(
    text: str,
    *,
    chat_id: int = CHAT_ID,
    message_id: int = 1,
    user: User | None = None,
    chat_type: str = "supergroup",
    title: str | None = "CS101",
    reply_to: Message | None = None,
    entities: list[MessageEntity] | None = None,
    date: datetime | None = None,
) -> Message:
    """Return a Telegram message shaped like a real group update."""

    chat = Chat(id=chat_id, type=chat_type, title=title)
    return Message(
        message_id=message_id,
        date=date or datetime.now(timezone.utc),
        chat=chat,
        from_user=user if user is not None else make_user(),
        text=text,
        reply_to_message=reply_to,
        entities=entities,
    )


def make_update(message: Message | None = None, **kwargs: Any) -> Update:
    """Return an update carrying ``message`` (or the supplied update kind)."""

    if message is not None:
        return Update(update_id=1, message=message)
    return Update(update_id=1, **kwargs)


def make_context(
    services: Any, *, args: list[str] | None = None, bot: AsyncMock | None = None
) -> SimpleNamespace:
    """Return a handler context backed by ``services``."""

    return SimpleNamespace(
        bot=bot or make_bot(),
        bot_data={"services": services},
        args=list(args or []),
        error=None,
    )


def make_callback_update(
    actor_id: int, data: str, bot: AsyncMock, *, chat_id: int = CHAT_ID
) -> Update:
    """Return an update for an inline-keyboard callback."""

    message = make_message(
        "Consent prompt",
        chat_id=chat_id,
        message_id=10,
        user=make_user(BOT_ID, username=BOT_USERNAME, is_bot=True),
    )
    query = CallbackQuery(
        id="callback-1",
        from_user=make_user(actor_id),
        chat_instance="chat-instance",
        message=message,
        data=data,
    )
    message.set_bot(bot)
    query.set_bot(bot)
    update = Update(update_id=2, callback_query=query)
    update.set_bot(bot)
    return update


def sent_text(context: SimpleNamespace) -> str:
    """Concatenate every text sent through the mocked bot."""

    return "".join(
        str(call.kwargs.get("text", ""))
        for call in context.bot.send_message.await_args_list
    )


def authorize(services: Any, chat_id: int = CHAT_ID) -> None:
    """Register and authorize a group through the real GroupConnector."""

    added = services.groups.on_bot_added(chat_id, "CS101", ADMIN_ID)
    assert added["status"] == "consent_required"
    granted = services.groups.on_authorize(chat_id, ADMIN_ID)
    assert granted["ok"] is True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def services() -> Any:
    """Provide BotServices on an isolated in-memory database."""

    database = Database(":memory:")
    built = build_services(make_settings(), db=database)
    try:
        yield built
    finally:
        database.close()


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def test_build_services_works_without_token_or_network() -> None:
    """build_services wires every module with no token and no network calls."""

    settings = Settings(
        telegram_bot_token="", admin_user_ids=(1,), bot_username=BOT_USERNAME
    )
    database = Database(":memory:")
    try:
        built = build_services(settings, db=database)

        assert built.settings is settings
        assert built.ingestor.cache is built.cache
        assert built.search.cache is built.cache
        assert asyncio.run(built.call(lambda value: value * 2, 21)) == 42
    finally:
        database.close()


def test_services_call_serializes_concurrent_work(services: Any) -> None:
    """Concurrent calls both complete, proving the lock does not deadlock."""

    async def scenario() -> list[int]:
        return await asyncio.gather(
            services.call(lambda: 1),
            services.call(lambda: 2),
            services.call(lambda: 3),
        )

    assert asyncio.run(scenario()) == [1, 2, 3]


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_short_and_empty() -> None:
    """Short text stays intact; empty input yields no chunks."""

    assert bot_handlers.chunk_text("hello") == ["hello"]
    assert bot_handlers.chunk_text("") == []
    assert bot_handlers.chunk_text("   \n\n  ") == []
    assert all(chunk.strip() for chunk in bot_handlers.chunk_text("\n\n\n"))


def test_chunk_text_preserves_paragraphs_when_they_fit() -> None:
    """Each fitting paragraph becomes its own chunk instead of being merged."""

    first = "a" * 3000
    second = "b" * 3000
    chunks = bot_handlers.chunk_text(f"{first}\n\n{second}")

    assert chunks == [first, second]


def test_chunk_text_hard_splits_long_lines_at_the_limit() -> None:
    """Oversized single lines are split at 4096 characters without loss."""

    text = "x" * 9000
    chunks = bot_handlers.chunk_text(text)

    assert len(chunks) == 3
    assert all(len(chunk) <= bot_handlers.CHAT_MESSAGE_LIMIT for chunk in chunks)
    assert "".join(chunks) == text


def test_chunk_text_respects_custom_limit() -> None:
    """A custom limit applies to both splitting and hard splits."""

    assert bot_handlers.chunk_text("abcdef", limit=3) == ["abc", "def"]
    assert bot_handlers.chunk_text("a\n\nb", limit=1) == ["a", "b"]


def test_reply_chunks_long_output(services: Any) -> None:
    """_reply never sends a message above Telegram's 4096-character limit."""

    context = make_context(services)
    text = "y" * 9000

    asyncio.run(bot_handlers._reply(context, CHAT_ID, text))

    calls = context.bot.send_message.await_args_list
    assert len(calls) == 3
    assert all(len(call.kwargs["text"]) <= 4096 for call in calls)
    assert "".join(call.kwargs["text"] for call in calls) == text


# ---------------------------------------------------------------------------
# Address detection
# ---------------------------------------------------------------------------


def test_is_addressed_to_bot_rules(services: Any) -> None:
    """Mentions and replies count; plain questions, bots and /ask do not."""

    settings = services.settings

    plain = make_update(make_message("What is the deadline?"))
    assert bot_handlers.is_addressed_to_bot(plain, settings) is False

    ask = make_update(make_message("/ask deadline"))
    assert bot_handlers.is_addressed_to_bot(ask, settings) is False

    mention = make_update(make_message(f"@{BOT_USERNAME} what is the deadline?"))
    assert bot_handlers.is_addressed_to_bot(mention, settings) is True

    bot_sender = make_update(
        make_message(
            f"@{BOT_USERNAME} automated note",
            user=make_user(BOT_ID, username=BOT_USERNAME, is_bot=True),
        )
    )
    assert bot_handlers.is_addressed_to_bot(bot_sender, settings) is False

    bot_reply = make_message(
        "My earlier answer",
        message_id=9,
        user=make_user(BOT_ID, username=BOT_USERNAME, is_bot=True),
    )
    replied = make_update(make_message("Can you clarify?", reply_to=bot_reply))
    assert bot_handlers.is_addressed_to_bot(replied, settings) is True

    entity_text = f"@{BOT_USERNAME} check this"
    entity = MessageEntity(
        type=MessageEntity.MENTION, offset=0, length=len(f"@{BOT_USERNAME}")
    )
    with_entity = make_update(make_message(entity_text, entities=[entity]))
    assert bot_handlers.is_addressed_to_bot(with_entity, settings) is True


# ---------------------------------------------------------------------------
# Consent and admin flow
# ---------------------------------------------------------------------------


def test_start_private_upserts_user_and_welcomes(services: Any) -> None:
    """A private /start records the profile and lists the commands."""

    context = make_context(services)
    message = make_message(
        "/start", chat_id=STUDENT_ID, chat_type="private", title=None
    )

    asyncio.run(bot_handlers.cmd_start(make_update(message), context))

    assert services.db.get_user(STUDENT_ID) is not None
    text = sent_text(context)
    assert "Welcome to JumnuyBot" in text
    assert "/ask" in text and "/today" in text


def test_bot_added_to_group_sends_consent_prompt_and_keyboard(services: Any) -> None:
    """The member update stores the group and offers an Approve button."""

    context = make_context(services)
    bot_user = make_user(BOT_ID, username=BOT_USERNAME, is_bot=True)
    member_update = ChatMemberUpdated(
        chat=Chat(id=CHAT_ID, type="supergroup", title="CS101"),
        from_user=make_user(ADMIN_ID, username="admin"),
        date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberLeft(bot_user),
        new_chat_member=ChatMemberMember(bot_user),
    )
    update = make_update(my_chat_member=member_update)

    asyncio.run(bot_handlers.on_my_chat_member(update, context))

    rows = services.db.query(
        "SELECT authorized FROM groups WHERE chat_id = ?", (CHAT_ID,)
    )
    assert rows and rows[0]["authorized"] == 0

    call = context.bot.send_message.await_args
    assert "/authorize" in call.kwargs["text"]
    keyboard = call.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "Approve"
    assert keyboard.inline_keyboard[0][0].callback_data == f"approve:{CHAT_ID}"


def test_approve_callback_authorizes_for_admin(services: Any) -> None:
    """An admin pressing Approve flips the consent flag and edits the prompt."""

    services.groups.on_bot_added(CHAT_ID, "CS101", ADMIN_ID)
    bot = make_bot()
    context = make_context(services, bot=bot)
    update = make_callback_update(ADMIN_ID, f"approve:{CHAT_ID}", bot)

    asyncio.run(bot_handlers.on_approve(update, context))

    assert services.db.is_group_authorized(CHAT_ID) is True
    answer = bot.answer_callback_query.await_args
    assert answer.kwargs["show_alert"] is False
    assert bot.edit_message_text.await_args is not None


def test_approve_callback_refuses_non_admin(services: Any) -> None:
    """A non-admin Approve shows an alert and leaves the group unauthorized."""

    services.groups.on_bot_added(CHAT_ID, "CS101", ADMIN_ID)
    bot = make_bot()
    context = make_context(services, bot=bot)
    update = make_callback_update(NON_ADMIN_ID, f"approve:{CHAT_ID}", bot)

    asyncio.run(bot_handlers.on_approve(update, context))

    assert services.db.is_group_authorized(CHAT_ID) is False
    answer = bot.answer_callback_query.await_args
    assert answer.kwargs["show_alert"] is True


def test_authorize_command_is_admin_only(services: Any) -> None:
    """Only a configured admin can authorize through /authorize."""

    services.groups.on_bot_added(CHAT_ID, "CS101", ADMIN_ID)
    admin_context = make_context(services)
    admin_update = make_update(make_message("/authorize", user=make_user(ADMIN_ID)))

    asyncio.run(bot_handlers.cmd_authorize(admin_update, admin_context))
    assert services.db.is_group_authorized(CHAT_ID) is True

    services.groups.on_bot_added(OTHER_CHAT_ID, "CS102", ADMIN_ID)
    student_context = make_context(services)
    student_update = make_update(
        make_message(
            "/authorize",
            chat_id=OTHER_CHAT_ID,
            user=make_user(NON_ADMIN_ID, username="student2"),
        )
    )

    asyncio.run(bot_handlers.cmd_authorize(student_update, student_context))
    assert services.db.is_group_authorized(OTHER_CHAT_ID) is False


def test_revoke_command_is_admin_only_and_keeps_messages(services: Any) -> None:
    """Revoking stops answering but never deletes stored messages."""

    authorize(services)
    services.ingestor.ingest(
        CHAT_ID,
        1,
        "The lab report deadline is 2026-12-01.",
        datetime.now(timezone.utc).isoformat(),
    )

    admin_context = make_context(services)
    asyncio.run(
        bot_handlers.cmd_revoke(
            make_update(make_message("/revoke", user=make_user(ADMIN_ID))),
            admin_context,
        )
    )

    assert services.db.is_group_authorized(CHAT_ID) is False
    assert len(services.db.list_messages(CHAT_ID)) == 1

    # A second revoke from a non-admin is refused and cannot re-authorize.
    services.groups.on_authorize(CHAT_ID, ADMIN_ID)
    student_context = make_context(services)
    asyncio.run(
        bot_handlers.cmd_revoke(
            make_update(
                make_message("/revoke", user=make_user(NON_ADMIN_ID, username="nobody"))
            ),
            student_context,
        )
    )
    assert services.db.is_group_authorized(CHAT_ID) is True
    assert len(services.db.list_messages(CHAT_ID)) == 1


# ---------------------------------------------------------------------------
# /ask and group text
# ---------------------------------------------------------------------------


def test_ask_returns_ranked_hits_with_numbered_links(services: Any) -> None:
    """Authorized /ask answers with numbered hits and Telegram links."""

    authorize(services)
    services.ingestor.ingest(
        CHAT_ID,
        1,
        "The submission deadline for the lab report is 2026-12-01.",
        datetime.now(timezone.utc).isoformat(),
    )
    context = make_context(services, args=["deadline"])
    update = make_update(make_message("/ask deadline"))

    asyncio.run(bot_handlers.cmd_ask(update, context))

    text = sent_text(context)
    assert "Top matches" in text
    assert "1. " in text
    assert "https://t.me/c/1234567890/1" in text


def test_ask_refuses_unauthorized_group(services: Any) -> None:
    """An unauthorized group gets the access refusal, not search results."""

    context = make_context(services, args=["deadline"])
    update = make_update(make_message("/ask deadline", chat_id=OTHER_CHAT_ID))

    asyncio.run(bot_handlers.cmd_ask(update, context))

    text = sent_text(context)
    assert "not authorized" in text.lower()
    assert "https://t.me" not in text


def test_unauthorized_group_stores_nothing_and_gets_no_reply(services: Any) -> None:
    """Plain text in an unauthorized group is silently dropped."""

    context = make_context(services)
    update = make_update(
        make_message("What is the deadline?", chat_id=OTHER_CHAT_ID)
    )

    asyncio.run(bot_handlers.on_group_text(update, context))

    assert services.db.list_messages(OTHER_CHAT_ID) == []
    assert context.bot.send_message.await_args_list == []


def test_plain_group_question_is_ingested_but_not_answered(services: Any) -> None:
    """Authorized groups store plain messages, yet only reply when addressed."""

    authorize(services)
    context = make_context(services)
    update = make_update(make_message("What is the deadline?", message_id=1))

    asyncio.run(bot_handlers.on_group_text(update, context))

    assert len(services.db.list_messages(CHAT_ID)) == 1
    assert context.bot.send_message.await_args_list == []


def test_mention_gets_a_reply_with_cited_hits(services: Any) -> None:
    """A mention searches stored messages and cites them with links."""

    authorize(services)
    services.ingestor.ingest(
        CHAT_ID,
        1,
        "The submission deadline for the lab report is 2026-12-01.",
        datetime.now(timezone.utc).isoformat(),
    )
    context = make_context(services)
    update = make_update(
        make_message(f"@{BOT_USERNAME} when is the deadline?", message_id=2)
    )

    asyncio.run(bot_handlers.on_group_text(update, context))

    text = sent_text(context)
    assert "Top matches" in text
    assert "https://t.me/c/1234567890/1" in text
    # The mention message itself is ingested before answering.
    assert len(services.db.list_messages(CHAT_ID)) == 2


def test_ask_without_hits_says_nothing_matched(services: Any) -> None:
    """An /ask query with no stored match gets an explicit notice."""

    authorize(services)
    services.ingestor.ingest(
        CHAT_ID,
        1,
        "The lab report deadline is 2026-12-01.",
        datetime.now(timezone.utc).isoformat(),
    )
    context = make_context(services, args=["zebra", "volleyball"])
    update = make_update(make_message("/ask zebra volleyball", message_id=2))

    asyncio.run(bot_handlers.cmd_ask(update, context))

    assert bot_handlers.NO_MATCHES in sent_text(context)


# ---------------------------------------------------------------------------
# /today
# ---------------------------------------------------------------------------


def test_today_returns_summary_sources_and_uncertainty(services: Any) -> None:
    """Today's conflicting deadlines produce a summary, sources and a warning."""

    authorize(services)
    now = datetime.now(timezone.utc).isoformat()
    services.ingestor.ingest(
        CHAT_ID,
        1,
        "Lab report submission deadline is 2026-12-01.",
        now,
    )
    services.ingestor.ingest(
        CHAT_ID,
        2,
        "Lab report submission deadline moved to 2026-12-08.",
        now,
    )
    context = make_context(services)
    update = make_update(make_message("/today", message_id=3))

    asyncio.run(bot_handlers.cmd_today(update, context))

    text = sent_text(context)
    assert "- " in text
    assert "Sources:" in text
    assert "https://t.me/c/1234567890/1" in text
    assert "uncertain" in text.lower()


def test_today_refuses_unauthorized_group(services: Any) -> None:
    """ /today respects the same consent gate as /ask."""

    context = make_context(services)
    update = make_update(make_message("/today", chat_id=OTHER_CHAT_ID))

    asyncio.run(bot_handlers.cmd_today(update, context))

    assert "not authorized" in sent_text(context).lower()


# ---------------------------------------------------------------------------
# Error handling and registration
# ---------------------------------------------------------------------------


def test_error_handler_replies_and_never_leaks_the_token(
    services: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The error handler replies safely, never raises and never logs the token."""

    token = "123456:TEST-token"
    bot = make_bot()
    context = SimpleNamespace(
        bot=bot,
        bot_data={"services": services},
        args=[],
        error=RuntimeError(f"Invalid token: {token}"),
    )
    update = make_update(make_message("boom"))

    with caplog.at_level("ERROR"):
        asyncio.run(bot_handlers.on_error(update, context))

    call = bot.send_message.await_args
    assert call.kwargs["text"] == bot_handlers.ERROR_TEXT
    assert token not in call.kwargs["text"]
    assert token not in caplog.text


def test_error_handler_tolerates_missing_update(services: Any) -> None:
    """An update-less error is logged and swallowed, not re-raised."""

    context = SimpleNamespace(
        bot=make_bot(),
        bot_data={"services": services},
        args=[],
        error=ValueError("no chat here"),
    )

    asyncio.run(bot_handlers.on_error(None, context))

    assert context.bot.send_message.await_args_list == []


class FakeApplication:
    """Minimal recorder implementing the Application handler API."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.error_handlers: list[Any] = []

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    def add_error_handler(self, handler: Any) -> None:
        self.error_handlers.append(handler)


def test_register_handlers_registers_every_command_and_error_handler() -> None:
    """register_handlers wires commands, membership, callbacks, text and errors."""

    app = FakeApplication()

    bot_handlers.register_handlers(app)

    commands: set[str] = set()
    for handler in app.handlers:
        if isinstance(handler, CommandHandler):
            commands.update(handler.commands)
    assert commands == {"start", "help", "ask", "today", "authorize", "revoke"}

    assert any(isinstance(h, ChatMemberHandler) for h in app.handlers)
    assert any(isinstance(h, CallbackQueryHandler) for h in app.handlers)
    assert any(isinstance(h, MessageHandler) for h in app.handlers)
    assert app.error_handlers == [bot_handlers.on_error]
