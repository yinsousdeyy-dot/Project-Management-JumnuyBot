"""Telegram message ingestion for JumnuyBot (SCRUM-32).

The ingestor is the single gateway through which Telegram messages reach the
``messages`` table. It enforces three invariants:

* **Privacy by default** -- only groups that have explicitly been authorized
  may store anything; unknown or revoked chats are silently refused.
* **Idempotence** -- ``(chat_id, message_id)`` is the identity of a message, so
  replaying an update never creates a second row.
* **Robustness** -- arbitrary/malformed Telegram payloads are rejected with
  ``None`` instead of raising, so a bad update can never crash the bot loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.config import Database
from src.models import Message

__all__ = ["MessageIngestor"]


def _coerce_int(value: Any) -> int | None:
    """Coerce a Telegram identifier (``int`` or numeric string) to ``int``.

    Args:
        value: Candidate identifier. Booleans are rejected on purpose because
            ``bool`` is an ``int`` subclass in Python.

    Returns:
        The integer value, or ``None`` when it cannot be coerced safely.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _epoch_to_iso(value: float) -> str | None:
    """Convert epoch seconds to a UTC ISO-8601 string, or ``None`` if invalid."""

    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _normalize_date(value: Any) -> str | None:
    """Normalize a Telegram date to an ISO-8601 UTC string.

    Accepts ``datetime`` instances, epoch numbers, numeric epoch strings and
    already-ISO strings (which are returned unchanged). Anything else -- or a
    value that cannot represent a real timestamp -- yields ``None``.
    """

    if isinstance(value, datetime):
        moment = (
            value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        )
        return moment.astimezone(timezone.utc).isoformat()
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _epoch_to_iso(value)
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            epoch = float(candidate)
        except ValueError:
            pass
        else:
            return _epoch_to_iso(epoch)
        try:
            datetime.fromisoformat(candidate)
        except ValueError:
            return None
        # An already-ISO timestamp is accepted exactly as supplied.
        return value
    return None


def _extract_chat_id(chat: Any) -> int | None:
    """Extract ``chat.id`` from a Telegram chat object or a bare identifier."""

    if isinstance(chat, dict):
        return _coerce_int(chat.get("id"))
    return _coerce_int(chat)


class MessageIngestor:
    """Validate, authorize and persist messages from Telegram (SCRUM-32).

    Args:
        db: Initialized :class:`~src.config.Database` instance.
        cache: Optional cache (for example ``MessageSearch``) exposing an
            ``invalidate(prefix)`` method. It is called after a successful
            store so stale search results do not survive new messages. The
            ingestor never imports or depends on the cache's concrete type.
    """

    def __init__(self, db: Database, cache: Any | None = None) -> None:
        self.db = db
        self.cache = cache

    def ingest(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        date: str,
        user_id: int = 0,
        sender_name: str = "",
    ) -> Message | None:
        """Store one message if its chat is authorized and it is not a duplicate.

        Args:
            chat_id: Telegram chat identifier; numeric strings are accepted.
            message_id: Per-chat Telegram message identifier.
            text: Message body. Empty or whitespace-only text is rejected.
            date: ISO-8601 timestamp, ``datetime``, or epoch seconds.
            user_id: Sender identifier; defaults to ``0`` when unknown.
            sender_name: Sender display name; empty when unknown.

        Returns:
            The persisted :class:`~src.models.Message` on success, or ``None``
            when the chat is unauthorized, the payload is invalid, or the
            message already exists.
        """

        chat = _coerce_int(chat_id)
        mid = _coerce_int(message_id)
        if chat is None or mid is None:
            return None

        # Privacy by default: unknown/revoked groups are never stored.
        if not self.db.is_group_authorized(chat):
            return None

        if not isinstance(text, str) or not text.strip():
            return None

        normalized_date = _normalize_date(date)
        if normalized_date is None:
            return None

        user = _coerce_int(user_id)
        message = Message(
            chat_id=chat,
            message_id=mid,
            text=text,
            date=normalized_date,
            user_id=user if user is not None else 0,
            sender_name=sender_name if isinstance(sender_name, str) else "",
        )

        if not self.db.add_message(message):
            # ``INSERT OR IGNORE`` reports no inserted row for duplicates.
            return None

        self._invalidate_cache(chat)
        return message

    def ingest_telegram_message(self, payload: dict[str, Any]) -> Message | None:
        """Parse a Telegram update or raw message dict and ingest it.

        Both ``{"message": {...}}`` updates and bare message dictionaries are
        accepted. Fields are normalized (chat ids to ``int``, dates to UTC
        ISO-8601, ``caption`` as a text fallback) and delegated to
        :meth:`ingest`, which applies authorization and dedupe rules.

        Args:
            payload: Telegram update/message mapping. Anything malformed is
                rejected without raising.

        Returns:
            The persisted message, or ``None`` for malformed/unauthorized/
            duplicate input.
        """

        try:
            if not isinstance(payload, dict):
                return None

            message = payload.get("message", payload)
            if not isinstance(message, dict):
                return None

            raw_id = message.get("message_id")
            if raw_id is None:
                raw_id = message.get("id")

            text = message.get("text")
            if text is None:
                text = message.get("caption")

            sender = message.get("from")
            user_id = 0
            sender_name = ""
            if isinstance(sender, dict):
                coerced_user = _coerce_int(sender.get("id"))
                user_id = coerced_user if coerced_user is not None else 0
                name = sender.get("first_name") or sender.get("username") or ""
                sender_name = name if isinstance(name, str) else ""

            return self.ingest(
                _extract_chat_id(message.get("chat")),
                _coerce_int(raw_id),
                text,
                message.get("date"),
                user_id=user_id,
                sender_name=sender_name,
            )
        except Exception:
            # Defensive: Telegram has many update variants; ingestion must not
            # crash the dispatcher on any unexpected shape.
            return None

    def _invalidate_cache(self, chat_id: int) -> None:
        """Best-effort search-cache invalidation after a successful store.

        ``MessageSearch`` caches results under ``search:{scope}:{query}:{limit}``
        keys, where scope is ``all`` for unscoped queries and the chat id for
        chat-scoped ones. Both prefixes are dropped so a newly ingested message
        can never be hidden behind a stale cached result set.
        """

        invalidate = getattr(self.cache, "invalidate", None)
        if not callable(invalidate):
            return
        for prefix in (f"search:{chat_id}:", "search:all:"):
            try:
                invalidate(prefix)
            except Exception:
                # The cache is an optional optimization; never fail ingestion.
                return
