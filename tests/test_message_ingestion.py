"""Tests for SCRUM-32 message ingestion.

All tests are offline and use the in-memory ``db`` fixture from conftest.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.config import Database
from src.message_ingestion import MessageIngestor
from src.models import Message

CHAT_ID = -100123
ISO_DATE = "2024-01-15T09:30:00+00:00"


def _authorize(db: Database, chat_id: int = CHAT_ID) -> None:
    """Grant consent for a chat so ingestion is allowed."""

    db.set_group_authorized(chat_id, True)


# ---------------------------------------------------------------------------
# ingest: core storage rules
# ---------------------------------------------------------------------------


def test_ingest_stores_authorized_message(db: Database) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)

    message = ingestor.ingest(
        CHAT_ID,
        10,
        "Homework due Friday",
        ISO_DATE,
        user_id=5,
        sender_name="Nok",
    )

    assert isinstance(message, Message)
    assert message.chat_id == CHAT_ID
    assert message.message_id == 10
    assert message.text == "Homework due Friday"
    assert message.date == ISO_DATE
    assert message.user_id == 5
    assert message.sender_name == "Nok"

    stored = db.get_message(CHAT_ID, 10)
    assert stored == message
    assert len(db.list_messages(CHAT_ID)) == 1


def test_duplicate_returns_none_and_keeps_single_row(db: Database) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)

    first = ingestor.ingest(CHAT_ID, 11, "Announcement", ISO_DATE)
    second = ingestor.ingest(CHAT_ID, 11, "Announcement", ISO_DATE)

    assert first is not None
    assert second is None
    assert len(db.list_messages(CHAT_ID)) == 1


def test_unauthorized_chat_rejected(db: Database) -> None:
    ingestor = MessageIngestor(db)

    # Never-seen chat: refused by default.
    assert ingestor.ingest(CHAT_ID, 12, "Secret", ISO_DATE) is None
    assert db.list_messages(CHAT_ID) == []

    # Explicitly revoked chat: refused too.
    db.set_group_authorized(CHAT_ID, False)
    assert ingestor.ingest(CHAT_ID, 12, "Secret", ISO_DATE) is None
    assert db.list_messages(CHAT_ID) == []


@pytest.mark.parametrize("text", ["", "   ", "\n\t  "])
def test_blank_text_rejected(db: Database, text: str) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)

    assert ingestor.ingest(CHAT_ID, 13, text, ISO_DATE) is None
    assert db.list_messages(CHAT_ID) == []


@pytest.mark.parametrize(
    ("chat_id", "message_id"),
    [
        (None, 1),
        ("not-an-int", 1),
        (CHAT_ID, None),
        (CHAT_ID, "not-an-int"),
        (True, 1),
        (CHAT_ID, True),
        (1.5, 1),
    ],
)
def test_uncoercible_ids_rejected(
    db: Database, chat_id: Any, message_id: Any
) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)

    assert ingestor.ingest(chat_id, message_id, "hello", ISO_DATE) is None


def test_numeric_string_ids_are_coerced(db: Database) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)

    message = ingestor.ingest(str(CHAT_ID), "77", "hello", ISO_DATE)

    assert message is not None
    assert message.chat_id == CHAT_ID
    assert message.message_id == 77


@pytest.mark.parametrize("date", [None, "", "   ", "not-a-date", object()])
def test_invalid_date_rejected(db: Database, date: Any) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)

    assert ingestor.ingest(CHAT_ID, 14, "hello", date) is None
    assert db.list_messages(CHAT_ID) == []


def test_epoch_date_is_normalized_to_utc_iso(db: Database) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)

    message = ingestor.ingest(CHAT_ID, 15, "hello", 1705311000)

    assert message is not None
    assert message.date == datetime.fromtimestamp(
        1705311000, tz=timezone.utc
    ).isoformat()


# ---------------------------------------------------------------------------
# ingest_telegram_message: payload shapes
# ---------------------------------------------------------------------------


def test_full_telegram_update_payload(db: Database) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)
    payload = {
        "update_id": 9001,
        "message": {
            "message_id": 42,
            "date": 1705311000,
            "chat": {"id": str(CHAT_ID), "type": "supergroup"},
            "from": {"id": 7, "first_name": "Nok", "username": "nok"},
            "caption": "Homework due Friday",
        },
    }

    message = ingestor.ingest_telegram_message(payload)

    assert message is not None
    assert message.chat_id == CHAT_ID
    assert message.message_id == 42
    assert message.text == "Homework due Friday"
    assert message.date == datetime.fromtimestamp(
        1705311000, tz=timezone.utc
    ).isoformat()
    assert message.user_id == 7
    assert message.sender_name == "Nok"
    assert db.get_message(CHAT_ID, 42) == message


def test_raw_message_dict_text_preferred_over_caption(db: Database) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)
    payload = {
        "chat": {"id": CHAT_ID},
        "message_id": 43,
        "date": ISO_DATE,
        "text": "text wins",
        "caption": "caption loses",
        "from": {"id": 9, "username": "somchai"},
    }

    message = ingestor.ingest_telegram_message(payload)

    assert message is not None
    assert message.text == "text wins"
    # Already-ISO dates are accepted as-is.
    assert message.date == ISO_DATE
    assert message.sender_name == "somchai"


def test_telegram_payload_without_sender_defaults(db: Database) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)
    payload = {
        "chat": {"id": CHAT_ID},
        "message_id": 44,
        "date": ISO_DATE,
        "text": "no sender info",
    }

    message = ingestor.ingest_telegram_message(payload)

    assert message is not None
    assert message.user_id == 0
    assert message.sender_name == ""


def test_telegram_payload_unauthorized_chat_rejected(db: Database) -> None:
    ingestor = MessageIngestor(db)
    payload = {
        "message": {
            "chat": {"id": CHAT_ID},
            "message_id": 45,
            "date": ISO_DATE,
            "text": "not allowed",
        }
    }

    assert ingestor.ingest_telegram_message(payload) is None
    assert db.list_messages(CHAT_ID) == []


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        [],
        "not-a-dict",
        123,
        {"message": None},
        {"message": "not-a-dict"},
        {"message": []},
        {"message": {"chat": None, "message_id": 1, "date": 1, "text": "x"}},
        {"message": {"chat": {"id": "bad"}, "message_id": 1, "date": 1, "text": "x"}},
        {"message": {"chat": {"id": CHAT_ID}, "date": 1, "text": "x"}},
        {
            "message": {
                "chat": {"id": CHAT_ID},
                "message_id": "bad",
                "date": 1,
                "text": "x",
            }
        },
        {
            "message": {
                "chat": {"id": CHAT_ID},
                "message_id": 1,
                "date": "not-a-date",
                "text": "x",
            }
        },
        {"message": {"chat": {"id": CHAT_ID}, "message_id": 1, "date": 1}},
    ],
)
def test_malformed_payloads_return_none_without_raising(
    db: Database, payload: Any
) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db)

    assert ingestor.ingest_telegram_message(payload) is None
    assert db.list_messages() == []


# ---------------------------------------------------------------------------
# Optional cache hook
# ---------------------------------------------------------------------------


class StubCache:
    """Minimal stand-in for ``MessageSearch`` exposing ``invalidate``."""

    def __init__(self) -> None:
        self.prefixes: list[str] = []

    def invalidate(self, prefix: str = "") -> int:
        self.prefixes.append(prefix)
        return 1


def test_cache_invalidation_called_after_successful_store(db: Database) -> None:
    _authorize(db)
    cache = StubCache()
    ingestor = MessageIngestor(db, cache=cache)

    assert ingestor.ingest(CHAT_ID, 20, "hello", ISO_DATE) is not None
    # Both MessageSearch key scopes must be dropped.
    assert cache.prefixes == [f"search:{CHAT_ID}:", "search:all:"]

    # Duplicates are not stored, so the cache is not invalidated again.
    assert ingestor.ingest(CHAT_ID, 20, "hello", ISO_DATE) is None
    assert cache.prefixes == [f"search:{CHAT_ID}:", "search:all:"]


def test_cache_without_invalidate_is_tolerated(db: Database) -> None:
    _authorize(db)
    ingestor = MessageIngestor(db, cache=object())

    assert ingestor.ingest(CHAT_ID, 21, "hello", ISO_DATE) is not None
    assert db.get_message(CHAT_ID, 21) is not None


def test_ingestion_invalidates_real_search_cache(db: Database) -> None:
    """A newly ingested message must appear in an already-populated search.

    The search module lands on its own branch, so this regression test skips
    (instead of failing at import time) when it is not part of the checkout.
    """

    message_search = pytest.importorskip(
        "src.message_search", reason="search module lives on its own branch"
    )

    _authorize(db)
    MessageIngestor(db).ingest(
        CHAT_ID, 30, "deadline is friday", "2026-11-18T00:00:00+00:00"
    )
    search = message_search.MessageSearch(db)

    # Populate both the global and chat-scoped caches first.
    before = search.search("deadline")
    assert [hit.message.message_id for hit in before] == [30]
    before_scoped = search.search("deadline", chat_id=CHAT_ID)
    assert [hit.message.message_id for hit in before_scoped] == [30]

    # Ingest a second matching message through a cache-aware ingestor.
    MessageIngestor(db, cache=search.cache).ingest(
        CHAT_ID, 31, "deadline moved to monday", "2026-11-18T01:00:00+00:00"
    )

    after = search.search("deadline")
    assert sorted(hit.message.message_id for hit in after) == [30, 31]
    # The chat-scoped cache must be refreshed too (not served stale).
    after_scoped = search.search("deadline", chat_id=CHAT_ID)
    assert sorted(hit.message.message_id for hit in after_scoped) == [30, 31]
