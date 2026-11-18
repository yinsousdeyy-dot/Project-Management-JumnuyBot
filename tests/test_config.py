"""Tests for the SCRUM-38 configuration and storage foundation."""

from __future__ import annotations

import dataclasses

import pytest

from src.config import (
    INDEXES,
    TABLE_SCHEMA,
    Database,
    Settings,
    load_settings,
)
from src.models import Message, SearchHit, SourceRef

EXPECTED_TABLES = {"users", "groups", "messages", "assignments", "reminders"}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_load_settings_defaults_when_env_empty() -> None:
    settings = load_settings({})

    assert settings == Settings()
    assert settings.telegram_bot_token == ""
    assert settings.rayu_api_key == ""
    assert settings.rayu_base_url == "https://gateway.rayucode.com/v1"
    assert settings.rayu_model == "deepseek-v4.1-flash-1017-cloud"
    assert settings.database_path == "jumnuybot.db"
    assert settings.cache_ttl_seconds == 300
    assert settings.default_timezone == "UTC"
    assert settings.reminder_lead_minutes == 60
    assert settings.admin_user_ids == ()
    assert settings.bot_username == "jumnuybot"


def test_load_settings_reads_all_env_vars() -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "token-from-env",
        "RAYU_API_KEY": "key-from-env",
        "RAYU_BASE_URL": "https://rayu.example.invalid/v1",
        "RAYU_MODEL": "model-from-env",
        "DATABASE_PATH": "/tmp/jumnuybot-test.db",
        "CACHE_TTL_SECONDS": "42",
        "DEFAULT_TIMEZONE": "Asia/Bangkok",
        "REMINDER_LEAD_MINUTES": "15",
        "ADMIN_USER_IDS": "1, 2,not-a-number,, 3",
        "BOT_USERNAME": "demo_bot",
    }

    settings = load_settings(env)

    assert settings.telegram_bot_token == "token-from-env"
    assert settings.rayu_api_key == "key-from-env"
    assert settings.rayu_base_url == "https://rayu.example.invalid/v1"
    assert settings.rayu_model == "model-from-env"
    assert settings.database_path == "/tmp/jumnuybot-test.db"
    assert settings.cache_ttl_seconds == 42
    assert settings.default_timezone == "Asia/Bangkok"
    assert settings.reminder_lead_minutes == 15
    assert settings.admin_user_ids == (1, 2, 3)
    assert settings.bot_username == "demo_bot"


@pytest.mark.parametrize(
    ("env", "expected_ttl", "expected_lead"),
    [
        ({"CACHE_TTL_SECONDS": "abc", "REMINDER_LEAD_MINUTES": "oops"}, 300, 60),
        ({"CACHE_TTL_SECONDS": "", "REMINDER_LEAD_MINUTES": "  "}, 300, 60),
        ({"CACHE_TTL_SECONDS": " 7 ", "REMINDER_LEAD_MINUTES": " 90 "}, 7, 90),
    ],
)
def test_load_settings_invalid_integers_fall_back(
    env: dict[str, str], expected_ttl: int, expected_lead: int
) -> None:
    settings = load_settings(env)

    assert settings.cache_ttl_seconds == expected_ttl
    assert settings.reminder_lead_minutes == expected_lead


def test_settings_is_frozen() -> None:
    settings = Settings()

    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.bot_username = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_covers_exactly_five_tables() -> None:
    assert set(TABLE_SCHEMA) == EXPECTED_TABLES
    for name, statement in TABLE_SCHEMA.items():
        assert "CREATE TABLE IF NOT EXISTS" in statement
        assert f"IF NOT EXISTS {name}" in statement


def test_initialize_creates_exactly_five_tables(db: Database) -> None:
    rows = db.query(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )

    assert len(rows) == 5
    assert {row["name"] for row in rows} == EXPECTED_TABLES


def test_initialize_creates_indexes(db: Database) -> None:
    names = {
        row["name"]
        for row in db.query(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
    }

    assert len(INDEXES) >= 3
    assert {
        "idx_messages_chat_date",
        "idx_assignments_due_date",
        "idx_reminders_remind_at",
    } <= names


def test_initialize_is_idempotent(db: Database) -> None:
    db.initialize()
    db.initialize()

    rows = db.query(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    assert len(rows) == 5


def test_memory_databases_are_isolated() -> None:
    first = Database(":memory:")
    second = Database(":memory:")
    try:
        first.upsert_user(1, username="only-in-first")
        assert first.get_user(1) is not None
        assert second.get_user(1) is None
    finally:
        first.close()
        second.close()


def test_file_database_persists_between_instances(tmp_path) -> None:
    path = tmp_path / "jumnuybot-test.db"

    first = Database(str(path))
    first.upsert_user(5, username="persisted")
    first.close()

    second = Database(str(path))
    try:
        user = second.get_user(5)
        assert user is not None
        assert user["username"] == "persisted"
    finally:
        second.close()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def test_user_upsert_and_get(db: Database) -> None:
    assert db.get_user(7) is None

    db.upsert_user(7, username="ann", timezone="Asia/Bangkok", alert_lead_minutes=30)
    user = db.get_user(7)

    assert user is not None
    assert user["user_id"] == 7
    assert user["username"] == "ann"
    assert user["timezone"] == "Asia/Bangkok"
    assert user["alert_lead_minutes"] == 30
    assert user["created_at"]

    created_at = user["created_at"]
    db.upsert_user(7, username="ann-updated", timezone="UTC", alert_lead_minutes=45)
    updated = db.get_user(7)

    assert updated is not None
    assert updated["username"] == "ann-updated"
    assert updated["timezone"] == "UTC"
    assert updated["alert_lead_minutes"] == 45
    assert updated["created_at"] == created_at


def test_user_defaults(db: Database) -> None:
    db.upsert_user(8)

    user = db.get_user(8)

    assert user is not None
    assert user["username"] == ""
    assert user["timezone"] == "UTC"
    assert user["alert_lead_minutes"] == 60


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


def test_group_authorization_flow(db: Database) -> None:
    assert db.is_group_authorized(-1001) is False

    db.upsert_group(-1001, title="CS101")
    assert db.is_group_authorized(-1001) is False

    db.set_group_authorized(-1001, True)
    assert db.is_group_authorized(-1001) is True

    db.set_group_authorized(-1001, False)
    assert db.is_group_authorized(-1001) is False


def test_group_upsert_updates_title_and_authorization(db: Database) -> None:
    db.upsert_group(-2002, title="Old title", authorized=False)
    db.upsert_group(-2002, title="New title", authorized=True)

    groups = db.query("SELECT * FROM groups WHERE chat_id = ?", (-2002,))

    assert len(groups) == 1
    assert groups[0]["title"] == "New title"
    assert db.is_group_authorized(-2002) is True


def test_set_group_authorized_creates_missing_group(db: Database) -> None:
    db.set_group_authorized(-3003, True)

    rows = db.query("SELECT * FROM groups WHERE chat_id = ?", (-3003,))
    assert len(rows) == 1
    assert rows[0]["authorized"] == 1


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_message_add_get_and_duplicate(db: Database) -> None:
    message = Message(
        chat_id=-1001,
        message_id=10,
        text="Homework due Friday",
        date="2026-01-10T09:00:00+00:00",
        user_id=7,
        sender_name="Ann",
    )

    assert db.add_message(message) is True
    assert db.add_message(message) is False  # duplicate primary key
    assert db.get_message(-1001, 10) == message
    assert db.get_message(-1001, 99) is None


def test_list_messages_filters_and_order(db: Database) -> None:
    first = Message(-1001, 10, "first", "2026-01-10T09:00:00+00:00", 7, "Ann")
    second = Message(-1001, 11, "second", "2026-02-01T09:00:00+00:00", 7, "Ann")
    other = Message(-1002, 1, "other chat", "2026-01-20T09:00:00+00:00", 8, "Bob")
    for message in (second, other, first):
        assert db.add_message(message) is True

    assert [m.message_id for m in db.list_messages(chat_id=-1001)] == [10, 11]
    assert [m.message_id for m in db.list_messages()] == [10, 1, 11]
    assert [
        m.message_id
        for m in db.list_messages(chat_id=-1001, since="2026-01-15T00:00:00+00:00")
    ] == [11]


def test_delete_messages_for_chat(db: Database) -> None:
    db.add_message(Message(-1001, 1, "a", "2026-01-01T00:00:00+00:00"))
    db.add_message(Message(-1001, 2, "b", "2026-01-02T00:00:00+00:00"))
    db.add_message(Message(-1002, 1, "c", "2026-01-03T00:00:00+00:00"))

    assert db.delete_messages_for_chat(-1001) == 2
    assert db.list_messages(chat_id=-1001) == []
    assert len(db.list_messages(chat_id=-1002)) == 1
    assert db.delete_messages_for_chat(-1001) == 0


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


def test_assignment_create_get_and_list(db: Database) -> None:
    assignment_id = db.add_assignment(
        "Essay",
        "2026-03-01T23:59:00+00:00",
        source_chat_id=-1001,
        source_message_id=10,
        owner_user_id=7,
    )
    other_id = db.add_assignment(
        "Lab report",
        "2026-03-02T23:59:00+00:00",
        source_chat_id=-1002,
        status="done",
    )

    assert isinstance(assignment_id, int)
    assert assignment_id > 0

    row = db.get_assignment(assignment_id)
    assert row is not None
    assert row["title"] == "Essay"
    assert row["due_date"] == "2026-03-01T23:59:00+00:00"
    assert row["source_chat_id"] == -1001
    assert row["source_message_id"] == 10
    assert row["owner_user_id"] == 7
    assert row["status"] == "candidate"
    assert row["created_at"]
    assert row["updated_at"]
    assert db.get_assignment(999_999) is None

    candidates = db.list_assignments(status="candidate")
    assert [item["id"] for item in candidates] == [assignment_id]

    in_other_chat = db.list_assignments(chat_id=-1002)
    assert [item["id"] for item in in_other_chat] == [other_id]


def test_assignment_reschedule_via_update(db: Database) -> None:
    assignment_id = db.add_assignment("Quiz", "2026-03-01T23:59:00+00:00")

    assert (
        db.update_assignment(
            assignment_id,
            due_date="2026-03-08T23:59:00+00:00",
            status="confirmed",
        )
        is True
    )

    row = db.get_assignment(assignment_id)
    assert row is not None
    assert row["due_date"] == "2026-03-08T23:59:00+00:00"
    assert row["status"] == "confirmed"
    assert row["updated_at"]


def test_update_assignment_ignores_unknown_fields(db: Database) -> None:
    assignment_id = db.add_assignment("Quiz", "2026-03-01T23:59:00+00:00")

    assert db.update_assignment(assignment_id, title="Quiz 1", evil_column="x") is True

    row = db.get_assignment(assignment_id)
    assert row is not None
    assert row["title"] == "Quiz 1"
    assert "evil_column" not in row


def test_update_and_delete_missing_assignment(db: Database) -> None:
    assert db.update_assignment(999_999, title="nope") is False
    assert db.delete_assignment(999_999) is False


def test_delete_assignment(db: Database) -> None:
    assignment_id = db.add_assignment("Lab", "2026-04-01T00:00:00+00:00")

    assert db.delete_assignment(assignment_id) is True
    assert db.get_assignment(assignment_id) is None
    assert db.delete_assignment(assignment_id) is False


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


def test_reminder_create_and_list(db: Database) -> None:
    assignment_id = db.add_assignment("Thesis", "2026-05-01T00:00:00+00:00")

    later = db.add_reminder(assignment_id, 7, "2026-04-30T09:00:00+00:00")
    earlier = db.add_reminder(
        assignment_id, 8, "2026-04-29T09:00:00+00:00", sent=1
    )

    assert isinstance(later, int) and later > 0
    assert isinstance(earlier, int) and earlier > 0
    assert later != earlier

    all_reminders = db.list_reminders()
    assert [row["id"] for row in all_reminders] == [earlier, later]
    assert all_reminders[0]["sent"] == 1
    assert all_reminders[1]["sent"] == 0
    assert all_reminders[0]["created_at"]

    user_reminders = db.list_reminders(user_id=7)
    assert len(user_reminders) == 1
    assert user_reminders[0]["id"] == later
    assert user_reminders[0]["assignment_id"] == assignment_id
    assert user_reminders[0]["remind_at"] == "2026-04-30T09:00:00+00:00"
    assert db.list_reminders(user_id=999) == []


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_message_row_roundtrip() -> None:
    message = Message(-1001, 10, "hello", "2026-01-10T09:00:00+00:00", 7, "Ann")

    row = message.to_row()
    assert row == {
        "chat_id": -1001,
        "message_id": 10,
        "text": "hello",
        "date": "2026-01-10T09:00:00+00:00",
        "user_id": 7,
        "sender_name": "Ann",
    }
    assert Message.from_row(row) == message
    assert Message.from_row(dict(row)) == message


def test_search_hit_and_source_ref() -> None:
    message = Message(-1001, 10, "hello", "2026-01-10T09:00:00+00:00")
    hit = SearchHit(message=message, score=1.5)

    assert hit.message == message
    assert hit.score == 1.5

    ref = SourceRef(
        chat_id=-1001,
        message_id=10,
        label="Ann: hello",
        url="https://t.me/c/1001/10",
    )
    assert ref.as_dict() == {
        "chat_id": -1001,
        "message_id": 10,
        "label": "Ann: hello",
        "url": "https://t.me/c/1001/10",
    }
