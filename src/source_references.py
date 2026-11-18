"""Telegram deep links, parsing and citation rendering (SCRUM-36).

The module is deliberately side-effect free and offline: it only turns
``(chat_id, message_id)`` pairs into ``t.me`` links, parses the link shapes
Telegram emits, and renders numbered source lists for summaries.

Link conventions
----------------
* Public chats/channels use ``https://t.me/<username>/<message_id>``.
* Supergroups (ids starting with ``-100``) use
  ``https://t.me/c/<id without the -100 prefix>/<message_id>``.
* Any other negative chat id uses its absolute value under ``/c/``. This is a
  best-effort private link because Telegram only resolves true supergroup ids.
* Positive chat ids without a username are emitted as ``/c/<chat_id>/...`` as a
  best effort; Telegram does not resolve those links for normal groups.

Parsing is intentionally forgiving and never raises: malformed input yields
``None``. ``parse_link`` only has chat ids in the URL, so a private ``/c/`` link
is always interpreted as a supergroup and returns ``-100<channel>``.
Public username links return ``0`` as the chat id sentinel because the numeric
chat id is not recoverable without a Bot API lookup.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.models import SourceRef

__all__ = ["format_source", "message_link", "parse_link", "render_sources"]

# Telegram's supergroup / channel id prefix.
_SUPERGROUP_PREFIX = "-100"
_TME_HOSTS = frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me"})


def _coerce_int(value: object) -> int | None:
    """Return ``value`` as an ``int`` or ``None`` when it is not integral.

    Booleans are rejected even though they are ``int`` subclasses. Strings are
    stripped before conversion so ``" 42 "`` is accepted.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text, 10)
        except ValueError:
            return None
    return None


def _normalize_username(username: object) -> str:
    """Return a bare Telegram username (no ``@``) or an empty string."""

    if not isinstance(username, str):
        return ""
    return username.strip().lstrip("@")


def message_link(chat_id: int | str, message_id: int | str, username: str = "") -> str:
    """Build a Telegram deep link for one message (SCRUM-36).

    Args:
        chat_id: Chat identifier. Supergroups use Telegram's ``-100...`` form;
            strings are accepted and stripped.
        message_id: Positive per-chat message identifier.
        username: Optional public username (with or without a leading ``@``).

    Returns:
        A ``https://t.me/...`` URL, or ``""`` when either id is empty, not an
        integer, zero, or the message id is not positive. This function never
        raises.

    Examples:
        >>> message_link(-1001234567890, 42)
        'https://t.me/c/1234567890/42'
        >>> message_link(-1001234567890, 42, "@jumnuy")
        'https://t.me/jumnuy/42'
    """

    chat = _coerce_int(chat_id)
    message = _coerce_int(message_id)
    if chat is None or message is None or chat == 0 or message <= 0:
        return ""

    handle = _normalize_username(username)
    if handle:
        return f"https://t.me/{handle}/{message}"

    chat_text = str(chat)
    if chat_text.startswith(_SUPERGROUP_PREFIX) and len(chat_text) > len(
        _SUPERGROUP_PREFIX
    ):
        channel = _coerce_int(chat_text[len(_SUPERGROUP_PREFIX) :])
        if channel is not None and channel > 0:
            return f"https://t.me/c/{channel}/{message}"

    if chat < 0:
        return f"https://t.me/c/{abs(chat)}/{message}"
    return f"https://t.me/c/{chat}/{message}"


def _channel_from_text(channel_text: str) -> int | None:
    """Convert a ``/c/`` channel fragment into a full supergroup chat id.

    ``"1234567890"`` becomes ``-1001234567890``; a fragment that already looks
    like a full negative id is returned unchanged. Returns ``None`` when the
    fragment is not an integer or is zero.
    """

    text = channel_text.strip()
    if not text:
        return None
    if text.startswith("-"):
        value = _coerce_int(text)
        return value if value not in (None, 0) else None
    if not text.isdigit():
        return None
    value = int(text)
    if value == 0:
        return None
    return int(f"{_SUPERGROUP_PREFIX}{text}")


def _parse_private_parts(parts: list[str]) -> tuple[int, int] | None:
    """Parse ``/c/<channel>/<message>`` URL path segments."""

    if len(parts) < 3:
        return None
    chat = _channel_from_text(parts[1])
    message = _coerce_int(parts[2])
    if chat is None or message is None or message <= 0:
        return None
    return chat, message


def _parse_public_parts(parts: list[str]) -> tuple[int, int] | None:
    """Parse ``/<username>/<message>`` URL path segments."""

    if len(parts) != 2:
        return None
    username = parts[0]
    message = _coerce_int(parts[1])
    if not username or message is None or message <= 0:
        return None
    return 0, message


def parse_link(link: str) -> tuple[int, int] | None:
    """Parse a Telegram message link into ``(chat_id, message_id)`` (SCRUM-36).

    Supported shapes:

    * ``https://t.me/c/<channel>/<message>`` -> ``(-100<channel>, message)``
    * ``https://t.me/<username>/<message>`` -> ``(0, message)`` (sentinel: the
      numeric chat id is unknown for public usernames)
    * ``tg://privatepost?channel=<channel>&post=<message>`` ->
      ``(-100<channel>, message)``

    ``http`` URLs, ``www.t.me`` hosts and trailing slashes are tolerated.
    Anything malformed, empty or non-string returns ``None``; this function
    never raises.
    """

    if not isinstance(link, str):
        return None
    text = link.strip()
    if not text:
        return None

    try:
        parsed = urlparse(text)
        scheme = parsed.scheme.lower()
        parts = [part for part in parsed.path.split("/") if part]

        if scheme == "tg":
            if parsed.netloc.lower() != "privatepost":
                return None
            query = parse_qs(parsed.query)
            channel_values = query.get("channel") or []
            post_values = query.get("post") or []
            if not channel_values or not post_values:
                return None
            chat = _channel_from_text(str(channel_values[0]))
            message = _coerce_int(str(post_values[0]))
            if chat is None or message is None or message <= 0:
                return None
            return chat, message

        if scheme not in {"http", "https"}:
            return None
        if parsed.netloc.lower() not in _TME_HOSTS:
            return None
        if not parts:
            return None
        if parts[0] == "c":
            return _parse_private_parts(parts)
        # ``/s/<username>/<message>`` is the public preview variant.
        if parts[0] == "s":
            return _parse_public_parts(parts[1:])
        return _parse_public_parts(parts)
    except (TypeError, ValueError):
        return None


def format_source(
    chat_id: int | str,
    message_id: int | str,
    username: str = "",
    index: int | None = None,
) -> str:
    """Format one citation as ``"[n] <url>"`` or just the URL (SCRUM-36).

    Args:
        chat_id: Chat identifier used for the link.
        message_id: Message identifier used for the link.
        username: Optional public username for the link.
        index: Optional 1-based source number. When ``None`` no prefix is added.

    Returns:
        The formatted citation, or ``""`` when no link can be built.
    """

    url = message_link(chat_id, message_id, username)
    if not url:
        return ""
    if index is None:
        return url
    return f"[{index}] {url}"


def _source_parts(
    item: object,
) -> tuple[object, object, object, object] | None:
    """Extract ``(chat_id, message_id, username, url)`` from a source item."""

    if isinstance(item, SourceRef):
        return item.chat_id, item.message_id, "", item.url
    if isinstance(item, Mapping):
        return (
            item.get("chat_id"),
            item.get("message_id"),
            item.get("username") or "",
            item.get("url") or "",
        )
    return None


def render_sources(sources: Iterable[Any] | None) -> str:
    """Render numbered source citations, one per line (SCRUM-36).

    Each item may be a :class:`~src.models.SourceRef` or a mapping with
    ``chat_id``/``message_id`` and optional ``username``/``url`` keys. An
    explicit non-empty ``url`` wins; otherwise the link is derived from the
    ids. Entries that cannot produce a link are skipped without consuming a
    number. ``None``, empty input or malformed containers yield ``""``; this
    function never raises.
    """

    if sources is None:
        return ""
    try:
        iterator = iter(sources)
    except TypeError:
        return ""

    lines: list[str] = []
    for item in iterator:
        parts = _source_parts(item)
        if parts is None:
            continue
        chat_id, message_id, username, url = parts

        text = url.strip() if isinstance(url, str) else ""
        if not text:
            if not isinstance(username, str):
                username = ""
            text = message_link(chat_id, message_id, username)  # type: ignore[arg-type]
        if not text:
            continue
        lines.append(f"[{len(lines) + 1}] {text}")

    return "\n".join(lines)
