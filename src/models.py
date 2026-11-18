"""Shared data models for JumnuyBot (SCRUM-38).

These dataclasses are the contract between storage and every feature module:
messages travel between ingestion, search and summarization as :class:`Message`
objects, search results are wrapped in :class:`SearchHit`, and citations are
represented by :class:`SourceRef`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

# ``sqlite3.Row`` is not a ``Mapping`` subclass but supports ``dict(row)``.
RowLike = dict[str, Any] | sqlite3.Row


@dataclass
class Message:
    """A Telegram message persisted in the ``messages`` table.

    Attributes:
        chat_id: Telegram chat (group) identifier.
        message_id: Per-chat Telegram message identifier.
        text: Plain message text.
        date: Timezone-aware ISO-8601 timestamp of the original message.
        user_id: Telegram user identifier of the sender, ``0`` when unknown.
        sender_name: Display name of the sender, empty when unknown.
    """

    chat_id: int
    message_id: int
    text: str
    date: str
    user_id: int = 0
    sender_name: str = ""

    def to_row(self) -> dict[str, Any]:
        """Return the message as a flat mapping suitable for SQL parameters."""

        return {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "text": self.text,
            "date": self.date,
            "user_id": self.user_id,
            "sender_name": self.sender_name,
        }

    @classmethod
    def from_row(cls, row: RowLike) -> "Message":
        """Build a :class:`Message` from a ``sqlite3.Row`` or plain mapping."""

        data = dict(row)
        return cls(
            chat_id=int(data["chat_id"]),
            message_id=int(data["message_id"]),
            text=str(data.get("text") or ""),
            date=str(data.get("date") or ""),
            user_id=int(data.get("user_id") or 0),
            sender_name=str(data.get("sender_name") or ""),
        )


@dataclass
class SearchHit:
    """A message matched by a search query together with its relevance score."""

    message: Message
    score: float


@dataclass
class SourceRef:
    """A citation pointing at a specific Telegram message."""

    chat_id: int
    message_id: int
    label: str = ""
    url: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the reference as a plain dictionary for JSON-like payloads."""

        return {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "label": self.label,
            "url": self.url,
        }
