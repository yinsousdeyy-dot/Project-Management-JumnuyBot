"""Telegram handler callables for the JumnuyBot group flow (SCRUM-45).

Every handler is a thin adapter over the Sprint 3 modules held in
:class:`~src.bot_services.BotServices` (read from
``context.bot_data["services"]``). The module owns three concerns:

* **Message chunking** -- Telegram rejects messages longer than 4096
  characters, so :func:`chunk_text` splits output on paragraph boundaries when
  possible and hard-splits otherwise.
* **Address detection** -- in groups the bot only answers when explicitly
  mentioned (``@botname``) or replied to, while messages are always ingested
  first so search and ``/today`` see the latest conversation.
* **Consent and admin flows** -- adding the bot returns a consent prompt with
  an inline Approve button; approving, ``/authorize`` and ``/revoke`` are
  all gated by the existing admin allow-list.

All handlers are async and fully offline-testable: they only touch the objects
stored in ``context.bot_data`` and the bot methods supplied by the test double.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.ext import (
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.source_references import message_link, render_sources

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids import cycles
    from src.bot_services import BotServices

__all__ = [
    "CHAT_MESSAGE_LIMIT",
    "chunk_text",
    "is_addressed_to_bot",
    "on_approve",
    "on_error",
    "on_group_text",
    "on_my_chat_member",
    "register_handlers",
]

logger = logging.getLogger(__name__)

#: Telegram's hard limit for a single text message, in characters.
CHAT_MESSAGE_LIMIT = 4096

#: Maximum length of a search-result excerpt.
HIT_EXCERPT_LIMIT = 160

#: Callback pattern emitted by the consent keyboard (registration contract).
_APPROVE_PATTERN = r"^approve:-?\d+$"
#: Parsing variant with a capture group for the chat id.
_APPROVE_PARSE_RE = re.compile(r"^approve:(-?\d+)$")

#: Statuses that mean the bot is now a (usable) member of the chat.
_ADDED_STATUSES = frozenset({"member", "administrator"})

#: Statuses that mean the bot was removed from the chat.
_REMOVED_STATUSES = frozenset({"left", "kicked"})

WELCOME_TEXT = (
    "Welcome to JumnuyBot! I help your class group find messages and "
    "announcements.\n\n"
    "Commands:\n"
    "/ask <question> - search class messages\n"
    "/today - summarize today's announcements\n"
    "/help - show this help\n\n"
    "Admins can enable the bot in a group with /authorize and disable it "
    "with /revoke."
)

HELP_TEXT = (
    "JumnuyBot help\n\n"
    "/ask <question> - search stored class messages and get cited links\n"
    "/today - summarize today's announcements with sources\n"
    "/help - show this help\n\n"
    "In a group, mention me (@{username}) or reply to one of my messages to "
    "ask a question. Groups must be authorized by an admin first. "
    "/authorize and /revoke are admin-only."
)

READY_TEXT = (
    "JumnuyBot is ready. Use /ask <question>, mention me, or send /today "
    "for today's announcements."
)

ERROR_TEXT = "Something went wrong. Please try again."

USAGE_ASK = "Usage: /ask <question>"

NO_MATCHES = "No matching messages found."

EMPTY_MENTION = "Please include a question after mentioning me."

UNCERTAINTY_NOTE = (
    "Note: some announcements may conflict, so this summary may be "
    "uncertain. Please verify against the linked messages."
)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def chunk_text(text: str, limit: int = CHAT_MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into non-empty chunks of at most ``limit`` characters.

    Paragraphs (separated by blank lines) are kept together when they fit, and
    long lines are hard-split so Telegram's 4096-character ceiling is never
    exceeded. Whitespace-only input yields an empty list; no returned chunk is
    empty or whitespace-only.

    Args:
        text: The text to split. Non-string values are coerced via ``str``;
            ``None`` is treated as empty.
        limit: Maximum chunk length. Values below 1 are clamped to 1.

    Returns:
        A list of non-empty strings, each at most ``limit`` characters long.
    """

    if text is None:
        return []
    if not isinstance(text, str):
        text = str(text)

    size = max(int(limit), 1)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return []

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current)
        current = ""

    for paragraph in normalized.split("\n\n"):
        if not paragraph.strip():
            # Collapse repeated blank lines; paragraphs already carry text.
            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= size:
            current = candidate
            continue

        flush()

        if len(paragraph) <= size:
            current = paragraph
            continue

        # Hard-split an oversized paragraph, preferring newline boundaries.
        remaining = paragraph
        while len(remaining) > size:
            split_at = remaining.rfind("\n", 0, size + 1)
            if split_at <= 0 or not remaining[:split_at].strip():
                split_at = size
            piece = remaining[:split_at].strip()
            if piece:
                chunks.append(piece)
            remaining = remaining[split_at:].lstrip("\n")
        if remaining.strip():
            current = remaining

    flush()
    return [chunk for chunk in chunks if chunk.strip()]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _services(context: ContextTypes.DEFAULT_TYPE) -> "BotServices":
    """Return the :class:`BotServices` stored on ``context.bot_data``."""

    return context.bot_data["services"]


def _chat_type(chat: Any) -> str:
    """Return a chat type as a lowercase string (``"private"``, ...)."""

    raw = getattr(chat, "type", "")
    return str(getattr(raw, "value", raw)).lower()


def _status(value: Any) -> str:
    """Return a chat-member status as a lowercase string."""

    return str(getattr(value, "value", value)).lower()


def _chat_id_of(target: Any) -> int | None:
    """Extract a chat id from an int, Update, Message or chat-like object."""

    if isinstance(target, int) and not isinstance(target, bool):
        return target

    direct = getattr(target, "chat_id", None)
    if isinstance(direct, int) and not isinstance(direct, bool):
        return direct

    chat = getattr(target, "effective_chat", None)
    if chat is None:
        chat = getattr(target, "chat", None)
    chat_id = getattr(chat, "id", None)
    if isinstance(chat_id, int) and not isinstance(chat_id, bool):
        return chat_id

    message = getattr(target, "effective_message", None)
    if message is not None:
        message_chat = getattr(message, "chat", None)
        message_chat_id = getattr(message_chat, "id", None)
        if isinstance(message_chat_id, int) and not isinstance(message_chat_id, bool):
            return message_chat_id
    return None


def _sender_id(update: Update) -> int | None:
    """Return the sending user's id, or ``None`` when unknown."""

    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    return user_id if isinstance(user_id, int) and not isinstance(user_id, bool) else None


def _bot_username(services: "BotServices") -> str:
    """Return the configured bot username without a leading ``@``."""

    return (getattr(services.settings, "bot_username", "") or "").strip().lstrip("@")


def _approve_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Build the one-button consent keyboard for ``chat_id``."""

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Approve", callback_data=f"approve:{int(chat_id)}")]]
    )


def _excerpt(text: str, limit: int = HIT_EXCERPT_LIMIT) -> str:
    """Collapse whitespace in ``text`` and cap it at ``limit`` characters."""

    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(limit - 3, 0)].rstrip() + "..."


async def _reply(context: ContextTypes.DEFAULT_TYPE, target: Any, text: str) -> None:
    """Send ``text`` to the chat behind ``target``, one chunk at a time."""

    chat_id = _chat_id_of(target)
    if chat_id is None:
        return
    for chunk in chunk_text(text):
        await context.bot.send_message(chat_id=chat_id, text=chunk)


def _ingest_payload(update: Update) -> dict[str, Any]:
    """Build the minimal Telegram-shaped payload the ingestor understands."""

    message = getattr(update, "effective_message", None)
    if message is None:
        message = getattr(update, "message", None)
    if message is None:
        return {"message": {}}

    chat = getattr(message, "chat", None)
    sender = getattr(message, "from_user", None)

    text = getattr(message, "text", None)
    if text is None:
        text = getattr(message, "caption", None)

    sender_payload: dict[str, Any] | None = None
    if sender is not None:
        sender_payload = {
            "id": getattr(sender, "id", 0),
            "first_name": getattr(sender, "first_name", "") or "",
            "username": getattr(sender, "username", "") or "",
            "is_bot": bool(getattr(sender, "is_bot", False)),
        }

    return {
        "message": {
            "message_id": getattr(message, "message_id", None),
            "date": getattr(message, "date", None),
            "text": text,
            "chat": {
                "id": getattr(chat, "id", None),
                "title": getattr(chat, "title", "") or "",
                "type": _chat_type(chat),
            },
            "from": sender_payload,
        }
    }


def _format_hits(hits: Iterable[Any]) -> str:
    """Render ranked search hits as numbered lines with links and excerpts."""

    lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        message = getattr(hit, "message", None)
        if message is None:
            continue
        excerpt = _excerpt(getattr(message, "text", "")) or "(no text)"
        date = str(getattr(message, "date", "") or "")
        link = message_link(
            getattr(message, "chat_id", 0), getattr(message, "message_id", 0)
        )
        entry = f"{index}. {excerpt}"
        if date:
            entry += f" ({date})"
        if link:
            entry += f"\n{link}"
        lines.append(entry)
    return "\n\n".join(lines)


def _strip_mention(text: str, services: "BotServices") -> str:
    """Remove the bot mention from ``text`` so it does not pollute the query."""

    username = _bot_username(services)
    if not username:
        return " ".join((text or "").split()).strip()
    cleaned = re.sub(
        rf"@{re.escape(username)}\b", " ", text or "", flags=re.IGNORECASE
    )
    return " ".join(cleaned.split()).strip()


# ---------------------------------------------------------------------------
# Address detection
# ---------------------------------------------------------------------------


def is_addressed_to_bot(update: Update, settings: Any) -> bool:
    """Return whether a group message explicitly targets the bot.

    Messages sent by bots are never addressed to it. Otherwise the bot is
    addressed when the text contains ``@<bot_username>`` (case-insensitive),
    when a ``MENTION`` entity resolves to that username, or when the message
    replies to one of the bot's own messages. ``/ask`` alone is *not* treated
    as an address; the command handler deals with it separately.

    Args:
        update: The incoming Telegram update.
        settings: Runtime settings providing ``bot_username``.

    Returns:
        ``True`` when the bot should answer, otherwise ``False``.
    """

    message = getattr(update, "effective_message", None)
    if message is None:
        message = getattr(update, "message", None)
    if message is None:
        return False

    sender = getattr(message, "from_user", None)
    if sender is not None and bool(getattr(sender, "is_bot", False)):
        return False

    username = (getattr(settings, "bot_username", "") or "").strip().lstrip("@")
    if not username:
        return False

    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    if f"@{username}".lower() in text.lower():
        return True

    entities: list[Any] = []
    for attribute in ("entities", "caption_entities"):
        entities.extend(getattr(message, attribute, None) or [])
    for entity in entities:
        if getattr(entity, "type", None) != MessageEntity.MENTION:
            continue
        offset = getattr(entity, "offset", 0) or 0
        length = getattr(entity, "length", 0) or 0
        token = text[offset : offset + length].strip().lstrip("@").lower()
        if token == username.lower():
            return True

    reply = getattr(message, "reply_to_message", None)
    if reply is not None:
        replied_sender = getattr(reply, "from_user", None)
        if replied_sender is not None and bool(
            getattr(replied_sender, "is_bot", False)
        ):
            replied_username = (
                getattr(replied_sender, "username", "") or ""
            ).strip().lstrip("@")
            if replied_username and replied_username.lower() == username.lower():
                return True

    return False


# ---------------------------------------------------------------------------
# Command and event handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start``: onboard private users, prompt groups for consent."""

    services = _services(context)
    message = getattr(update, "effective_message", None)
    chat = getattr(update, "effective_chat", None)
    chat_id = _chat_id_of(update)
    if chat_id is None:
        return

    if _chat_type(chat) == "private":
        user_id = _sender_id(update)
        if user_id is not None:
            username = (getattr(getattr(update, "effective_user", None), "username", "") or "")
            await services.call(services.db.upsert_user, user_id, username)
        await _reply(context, chat_id, WELCOME_TEXT)
        return

    authorized = await services.call(services.access.is_authorized_group, chat_id)
    if authorized:
        await _reply(context, chat_id, READY_TEXT)
        return

    prompt = services.groups.consent_prompt(chat_id)
    await context.bot.send_message(
        chat_id=chat_id, text=prompt, reply_markup=_approve_keyboard(chat_id)
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help`` with the command reference."""

    services = _services(context)
    username = _bot_username(services)
    await _reply(context, _chat_id_of(update), HELP_TEXT.format(username=username))


async def on_my_chat_member(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle bot membership changes: request consent when added to a group."""

    services = _services(context)
    member_update = getattr(update, "my_chat_member", None)
    if member_update is None:
        return

    chat = getattr(member_update, "chat", None)
    chat_id = getattr(chat, "id", None)
    if not isinstance(chat_id, int):
        return

    new_member = getattr(member_update, "new_chat_member", None)
    subject = getattr(new_member, "user", None)
    if subject is None or not _is_bot_itself(subject, context, services):
        return

    status = _status(getattr(new_member, "status", ""))
    if status in _REMOVED_STATUSES:
        logger.info("Bot removed from chat %s", chat_id)
        return
    if status not in _ADDED_STATUSES:
        return

    actor = getattr(member_update, "from_user", None)
    actor_id = getattr(actor, "id", 0) or 0
    title = getattr(chat, "title", "") or ""

    result = await services.call(services.groups.on_bot_added, chat_id, title, actor_id)
    message = str(result.get("message", ""))
    if result.get("status") == "consent_required":
        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=_approve_keyboard(chat_id),
        )
    else:
        await _reply(context, chat_id, message)


def _is_bot_itself(
    user: Any, context: ContextTypes.DEFAULT_TYPE, services: "BotServices"
) -> bool:
    """Return whether ``user`` is the bot running this handler."""

    bot_id = getattr(context.bot, "id", None)
    if isinstance(bot_id, int) and getattr(user, "id", None) == bot_id:
        return True
    username = (getattr(user, "username", "") or "").strip().lstrip("@").lower()
    expected = _bot_username(services).lower()
    return bool(getattr(user, "is_bot", False) and username and expected == username)


async def on_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline Approve button from the consent prompt."""

    services = _services(context)
    query = getattr(update, "callback_query", None)
    if query is None:
        return

    data = getattr(query, "data", "") or ""
    match = _APPROVE_PARSE_RE.match(data)
    if match is None:
        await query.answer()
        return

    chat_id = int(match.group(1))
    actor = getattr(query, "from_user", None)
    actor_id = getattr(actor, "id", 0) or 0

    result = await services.call(services.groups.on_authorize, chat_id, actor_id)
    ok = bool(result.get("ok"))
    message = str(result.get("message", ""))

    await query.answer(text=message, show_alert=not ok)
    try:
        await query.edit_message_text(message)
    except Exception:  # pragma: no cover - best effort; message may be old
        logger.debug("Could not edit consent message in chat %s", chat_id)


async def cmd_authorize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/authorize`` (admin-only, enforced by GroupConnector)."""

    services = _services(context)
    chat_id = _chat_id_of(update)
    if chat_id is None:
        return
    result = await services.call(
        services.groups.on_authorize, chat_id, _sender_id(update)
    )
    await _reply(context, chat_id, str(result.get("message", "")))


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/revoke`` (admin-only; stored messages are kept)."""

    services = _services(context)
    chat_id = _chat_id_of(update)
    if chat_id is None:
        return
    result = await services.call(
        services.groups.on_revoke, chat_id, _sender_id(update)
    )
    await _reply(context, chat_id, str(result.get("message", "")))


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/ask <question>``: access check, search, cited results."""

    services = _services(context)
    chat_id = _chat_id_of(update)
    if chat_id is None:
        return

    query = " ".join(context.args or []).strip()
    if not query:
        await _reply(context, chat_id, USAGE_ASK)
        return

    allowed, reason = await services.call(
        services.access.check_query, _sender_id(update), chat_id
    )
    if not allowed:
        await _reply(context, chat_id, reason)
        return

    hits = await services.call(services.search.search, query, chat_id, 10)
    if not hits:
        await _reply(context, chat_id, NO_MATCHES)
        return

    body = f"Top matches for \u201c{query}\u201d:\n\n{_format_hits(hits)}"
    await _reply(context, chat_id, body)


async def on_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ingest every group message, then answer only when explicitly addressed."""

    services = _services(context)
    message = getattr(update, "effective_message", None)
    if message is None:
        return

    chat_id = _chat_id_of(update)
    if chat_id is None:
        return

    # Privacy rule: ingestion itself refuses unauthorized groups, but we always
    # offer the message so an authorized group never misses an update.
    payload = _ingest_payload(update)
    await services.call(services.ingestor.ingest_telegram_message, payload)

    if not is_addressed_to_bot(update, services.settings):
        return

    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    query = _strip_mention(text, services)
    if not query:
        await _reply(context, chat_id, EMPTY_MENTION)
        return

    allowed, reason = await services.call(
        services.access.check_query, _sender_id(update), chat_id
    )
    if not allowed:
        await _reply(context, chat_id, reason)
        return

    hits = await services.call(services.search.search, query, chat_id, 10)
    if not hits:
        await _reply(context, chat_id, NO_MATCHES)
        return

    body = f"Top matches for \u201c{query}\u201d:\n\n{_format_hits(hits)}"
    await _reply(context, chat_id, body)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/today``: today's summary, cited sources and uncertainty note."""

    services = _services(context)
    chat_id = _chat_id_of(update)
    if chat_id is None:
        return

    allowed, reason = await services.call(
        services.access.check_query, _sender_id(update), chat_id
    )
    if not allowed:
        await _reply(context, chat_id, reason)
        return

    result = await services.call(services.summarizer.summarize_today, chat_id)
    result = result or {}

    text = str(result.get("summary") or "")
    rendered_sources = render_sources(result.get("sources") or [])
    if rendered_sources:
        text = f"{text}\n\nSources:\n{rendered_sources}".strip()
    if result.get("uncertain"):
        text = f"{text}\n\n{UNCERTAINTY_NOTE}".strip()

    await _reply(context, chat_id, text)


async def on_error(update: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log an update-processing failure and reply briefly, never re-raising.

    Exception text is deliberately not logged because Telegram errors such as
    ``InvalidToken`` embed the token in their message.
    """

    error = getattr(context, "error", None)
    logger.error(
        "Unhandled error while processing an update (%s)", type(error).__name__
    )

    chat_id = _chat_id_of(update) if update is not None else None
    if chat_id is None:
        return
    try:
        await context.bot.send_message(chat_id=chat_id, text=ERROR_TEXT)
    except Exception:  # pragma: no cover - Telegram may be unreachable
        logger.debug("Could not deliver the error notice to chat %s", chat_id)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_handlers(app: Any) -> None:
    """Attach every command, callback, membership and group-text handler."""

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("authorize", cmd_authorize))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    app.add_handler(CallbackQueryHandler(on_approve, pattern=_APPROVE_PATTERN))
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
            on_group_text,
        )
    )
    app.add_error_handler(on_error)
