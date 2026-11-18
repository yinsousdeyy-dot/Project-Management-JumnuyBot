"""End-to-end reliability tests for Sprint 3 (SCRUM-37).

These tests exercise every Sprint 3 module together -- group consent,
ingestion, search, summarization, access control and source links -- against
one in-memory database. They are fully offline: the AI client is a local fake,
``requests`` is never invoked, and all storage uses ``Database(':memory:')``.

The suite covers the cross-module happy path plus the reliability edge cases
the sprint calls out: privacy refusal, empty results, cache reuse and
invalidation, deadline rescheduling (JUM-103), the five-table schema and the
revoke path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.access_control import AccessController
from src.announcement_summary import AnnouncementSummarizer
from src.config import Database, Settings
from src.group_connector import GroupConnector
from src.message_ingestion import MessageIngestor
from src.message_search import MessageSearch
from src.source_references import message_link

#: A Telegram supergroup id: links for these use the ``/c/<channel>/`` form.
SUPERGROUP_CHAT_ID = -1001234567890

#: The admin user configured by the shared ``settings`` fixture.
ADMIN_USER_ID = 1

#: The deadline message is the newest one, so it wins search on score and date.
DEADLINE_MESSAGE_ID = 1


def telegram_message(
    chat_id: int,
    message_id: int,
    text: str,
    date: str,
    user_id: int = 500,
    sender_name: str = "Student",
) -> dict[str, Any]:
    """Build a minimal Telegram update payload for the ingestor.

    Args:
        chat_id: Telegram chat identifier.
        message_id: Per-chat message identifier.
        text: Message body.
        date: ISO-8601 timestamp understood by the ingestor.
        user_id: Sender identifier.
        sender_name: Sender display name.

    Returns:
        A ``{"update_id": ..., "message": {...}}`` mapping shaped like a real
        Telegram update, with no network involvement.
    """

    return {
        "update_id": 10_000 + message_id,
        "message": {
            "message_id": message_id,
            "date": date,
            "text": text,
            "chat": {"id": chat_id, "type": "supergroup", "title": "CS101"},
            "from": {"id": user_id, "first_name": sender_name},
        },
    }


def authorize_group(db: Database, settings: Settings, chat_id: int) -> GroupConnector:
    """Run the add-then-authorize consent handshake for ``chat_id``.

    Returns:
        The :class:`GroupConnector` used, so callers can revoke later.
    """

    connector = GroupConnector(db, settings)
    added = connector.on_bot_added(chat_id, "CS101 Study Group", ADMIN_USER_ID)
    assert added["status"] == "consent_required"
    granted = connector.on_authorize(chat_id, ADMIN_USER_ID)
    assert granted["ok"] is True
    assert db.is_group_authorized(chat_id) is True
    return connector


class FakeClient:
    """Offline stand-in for ``RayuClient`` that never touches the network."""

    def __init__(self, result: str = "Lab report deadline is 1 December 2026 [1].") -> None:
        self.result = result
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, prompt: str) -> str:
        """Record the prompt and return the canned AI answer."""

        self.calls += 1
        self.prompts.append(prompt)
        return self.result


class CountingDatabase:
    """Proxy around :class:`Database` that counts ``list_messages`` reads.

    ``MessageSearch`` only requires ``list_messages``, so counting calls to
    that one method proves whether a query was served from cache or refetched.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self.list_messages_calls = 0

    def list_messages(
        self, chat_id: int | None = None, since: str | None = None
    ) -> list[Any]:
        """Count the read, then delegate to the real database."""

        self.list_messages_calls += 1
        return self._db.list_messages(chat_id, since)


# ---------------------------------------------------------------------------
# 1. Happy path journey
# ---------------------------------------------------------------------------


def test_end_to_end_consent_ingest_search_summarize_and_link(
    db: Database, settings: Settings
) -> None:
    """SCRUM-37 happy path across consent, ingestion, search, summary and links."""

    connector = GroupConnector(db, settings)
    added = connector.on_bot_added(SUPERGROUP_CHAT_ID, "CS101 Study Group", ADMIN_USER_ID)
    assert added["status"] == "consent_required"

    granted = connector.on_authorize(SUPERGROUP_CHAT_ID, ADMIN_USER_ID)
    assert granted["ok"] is True
    assert db.is_group_authorized(SUPERGROUP_CHAT_ID) is True

    ingestor = MessageIngestor(db)
    payloads = [
        telegram_message(
            SUPERGROUP_CHAT_ID,
            DEADLINE_MESSAGE_ID,
            "The submission deadline for the lab report is 2026-12-01.",
            "2026-11-18T10:00:00+00:00",
        ),
        telegram_message(
            SUPERGROUP_CHAT_ID,
            2,
            "The exam schedule is posted on the department board.",
            "2026-11-17T09:00:00+00:00",
        ),
        telegram_message(
            SUPERGROUP_CHAT_ID,
            3,
            "The library closes at 18:00 during exam week.",
            "2026-11-16T08:00:00+00:00",
        ),
    ]
    stored = [ingestor.ingest_telegram_message(payload) for payload in payloads]
    assert all(message is not None for message in stored)
    assert len(db.list_messages(SUPERGROUP_CHAT_ID)) == 3

    hits = MessageSearch(db).search("when is the deadline")
    assert hits, "the deadline query should match stored announcements"
    assert hits[0].message.message_id == DEADLINE_MESSAGE_ID
    assert "deadline" in hits[0].message.text.lower()

    client = FakeClient()
    summary = AnnouncementSummarizer(settings, db=db, client=client).summarize(
        [hit.message for hit in hits]
    )
    assert summary["used_ai"] is True
    assert summary["sources"], "a non-empty summary must cite its sources"
    assert summary["sources"][0]["message_id"] == DEADLINE_MESSAGE_ID
    assert client.calls == 1

    top_source = summary["sources"][0]
    link = message_link(top_source["chat_id"], top_source["message_id"])
    assert link == "https://t.me/c/1234567890/1"
    assert link.startswith("https://t.me/c/")


# ---------------------------------------------------------------------------
# 2. Privacy refusal
# ---------------------------------------------------------------------------


def test_unauthorized_group_refuses_ingestion_and_queries(
    db: Database, settings: Settings
) -> None:
    """SCRUM-37 privacy: an unauthorized group stores nothing and is refused."""

    unauthorized_chat_id = -1009999999999
    ingestor = MessageIngestor(db)

    direct = ingestor.ingest(
        unauthorized_chat_id,
        1,
        "This class group was never authorized.",
        "2026-11-18T09:00:00+00:00",
    )
    assert direct is None

    from_payload = ingestor.ingest_telegram_message(
        telegram_message(
            unauthorized_chat_id,
            2,
            "Another message that must not be stored.",
            "2026-11-18T09:05:00+00:00",
        )
    )
    assert from_payload is None

    allowed, reason = AccessController(settings, db).check_query(
        ADMIN_USER_ID, unauthorized_chat_id
    )
    assert allowed is False
    assert reason, "a refusal must explain why the query was rejected"

    assert db.list_messages(unauthorized_chat_id) == []
    assert db.list_messages() == []


# ---------------------------------------------------------------------------
# 3. Empty-result path
# ---------------------------------------------------------------------------


def test_empty_search_and_summary_are_explicit_and_safe(
    db: Database, settings: Settings
) -> None:
    """SCRUM-37 empty results: no matches means empty list and a clear message."""

    authorize_group(db, settings, SUPERGROUP_CHAT_ID)
    ingestor = MessageIngestor(db)
    ingestor.ingest_telegram_message(
        telegram_message(
            SUPERGROUP_CHAT_ID,
            1,
            "The lab report deadline is 2026-12-01.",
            "2026-11-18T10:00:00+00:00",
        )
    )
    ingestor.ingest_telegram_message(
        telegram_message(
            SUPERGROUP_CHAT_ID,
            2,
            "The exam schedule is posted.",
            "2026-11-17T09:00:00+00:00",
        )
    )

    assert MessageSearch(db).search("zebra volleyball tournament") == []

    client = FakeClient("should never be used for an empty batch")
    summary = AnnouncementSummarizer(settings, db=db, client=client).summarize([])
    assert "No relevant announcements" in summary["summary"]
    assert summary["summary"] == "No relevant announcements were found for today."
    assert summary["sources"] == []
    assert summary["used_ai"] is False
    assert client.calls == 0


# ---------------------------------------------------------------------------
# 4. Cache behavior
# ---------------------------------------------------------------------------


def test_second_search_is_cached_and_invalidate_forces_refetch(
    db: Database, settings: Settings
) -> None:
    """SCRUM-37 cache: repeated searches reuse cache; invalidation refetches."""

    authorize_group(db, settings, SUPERGROUP_CHAT_ID)
    MessageIngestor(db).ingest_telegram_message(
        telegram_message(
            SUPERGROUP_CHAT_ID,
            1,
            "The lab report deadline is 2026-12-01.",
            "2026-11-18T10:00:00+00:00",
        )
    )

    counting_db = CountingDatabase(db)
    search = MessageSearch(counting_db, ttl_seconds=300)

    first = search.search("deadline")
    assert counting_db.list_messages_calls == 1
    assert first and first[0].message.message_id == 1

    second = search.search("deadline")
    assert counting_db.list_messages_calls == 1, "identical search must hit the cache"
    assert [hit.message.message_id for hit in second] == [
        hit.message.message_id for hit in first
    ]

    removed = search.invalidate_cache()
    assert removed >= 1

    third = search.search("deadline")
    assert counting_db.list_messages_calls == 2, "invalidated search must refetch"
    assert [hit.message.message_id for hit in third] == [
        hit.message.message_id for hit in first
    ]


# ---------------------------------------------------------------------------
# 5. Deadline reschedule (JUM-103)
# ---------------------------------------------------------------------------


def test_deadline_reschedule_updates_due_date_and_updated_at(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCRUM-37 / JUM-103: rescheduling changes due_date and refreshes updated_at.

    The storage clock is patched to two fixed, increasing instants so the
    assertion on ``updated_at`` is deterministic rather than second-granularity
    dependent.
    """

    start = datetime(2026, 11, 18, 10, 0, 0, tzinfo=timezone.utc)
    stamps = iter(
        [
            start.isoformat(timespec="seconds"),
            (start + timedelta(minutes=5)).isoformat(timespec="seconds"),
        ]
    )
    monkeypatch.setattr("src.config._utc_now", lambda: next(stamps))

    assignment_id = db.add_assignment(
        title="Lab report",
        due_date="2026-12-01",
        source_chat_id=SUPERGROUP_CHAT_ID,
        source_message_id=DEADLINE_MESSAGE_ID,
        owner_user_id=500,
        status="candidate",
    )
    before = db.get_assignment(assignment_id)
    assert before is not None
    assert before["due_date"] == "2026-12-01"
    assert before["status"] == "candidate"

    assert db.update_assignment(assignment_id, due_date="2026-12-08") is True

    after = db.get_assignment(assignment_id)
    assert after is not None
    assert after["due_date"] == "2026-12-08"
    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] != before["updated_at"]
    assert after["updated_at"] > before["updated_at"]

    listed = db.list_assignments(status="candidate")
    assert [row["id"] for row in listed] == [assignment_id]


# ---------------------------------------------------------------------------
# 6. Five-table schema
# ---------------------------------------------------------------------------


def test_initialize_creates_exactly_the_five_tables(db: Database) -> None:
    """SCRUM-37 foundation check: the schema has exactly the five Sprint 3 tables."""

    db.initialize()  # idempotent second call must not change the table set
    names = {
        row["name"]
        for row in db.query(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names == {"users", "groups", "messages", "assignments", "reminders"}


# ---------------------------------------------------------------------------
# 7. Revoke path
# ---------------------------------------------------------------------------


def test_revoke_stops_ingestion_and_keeps_existing_history(
    db: Database, settings: Settings
) -> None:
    """SCRUM-37 revoke: after consent is withdrawn, new messages are refused."""

    connector = authorize_group(db, settings, SUPERGROUP_CHAT_ID)
    ingestor = MessageIngestor(db)

    first = ingestor.ingest_telegram_message(
        telegram_message(
            SUPERGROUP_CHAT_ID,
            1,
            "The lab report deadline is 2026-12-01.",
            "2026-11-18T10:00:00+00:00",
        )
    )
    assert first is not None

    revoked = connector.on_revoke(SUPERGROUP_CHAT_ID, ADMIN_USER_ID)
    assert revoked["ok"] is True
    assert db.is_group_authorized(SUPERGROUP_CHAT_ID) is False

    refused = ingestor.ingest_telegram_message(
        telegram_message(
            SUPERGROUP_CHAT_ID,
            2,
            "This message arrives after consent was revoked.",
            "2026-11-18T11:00:00+00:00",
        )
    )
    assert refused is None
    assert (
        ingestor.ingest(
            SUPERGROUP_CHAT_ID,
            3,
            "Also refused.",
            "2026-11-18T11:05:00+00:00",
        )
        is None
    )

    stored = db.list_messages(SUPERGROUP_CHAT_ID)
    assert [message.message_id for message in stored] == [1]
    assert AccessController(settings, db).check_query(500, SUPERGROUP_CHAT_ID)[0] is False
