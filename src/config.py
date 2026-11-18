"""Central configuration and SQLite storage foundation (SCRUM-38).

This module is the single source of truth for runtime settings and for the
five-table persistence layer used by every Sprint 3 feature module. It is
offline-safe: no network access happens here, and secrets are only ever read
from the environment (or an optional ``.env`` file), never hard-coded.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from src.models import Message

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration assembled from environment variables."""

    telegram_bot_token: str = ""
    rayu_api_key: str = ""
    rayu_base_url: str = "https://gateway.rayucode.com/v1"
    rayu_model: str = "deepseek-v4.1-flash-1017-cloud"
    database_path: str = "jumnuybot.db"
    cache_ttl_seconds: int = 300
    default_timezone: str = "UTC"
    reminder_lead_minutes: int = 60
    admin_user_ids: tuple[int, ...] = ()
    bot_username: str = "jumnuybot"


def _load_dotenv_if_available() -> None:
    """Load ``.env`` when python-dotenv is installed.

    ``python-dotenv`` is an optional convenience for local development. A
    missing dependency is not an error: values simply come from the real
    process environment.
    """

    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        load_dotenv()
    except Exception:  # pragma: no cover - defensive, dotenv should not break
        return


def _env_text(env: dict[str, str], key: str, default: str) -> str:
    """Read ``key`` from ``env`` as stripped text, falling back to ``default``."""

    value = env.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    """Read ``key`` from ``env`` as an integer, falling back to ``default``."""

    try:
        return int(_env_text(env, key, str(default)))
    except (TypeError, ValueError):
        return default


def _parse_admin_user_ids(value: str) -> tuple[int, ...]:
    """Parse a comma-separated admin id list, ignoring blanks and junk."""

    ids: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return tuple(ids)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from a mapping or the process environment.

    Args:
        env: Optional explicit environment mapping. When ``None`` the
            process environment is used and an optional ``.env`` file is
            loaded first if python-dotenv is available.

    Returns:
        A frozen :class:`Settings` instance with parsed values and defaults.
    """

    if env is None:
        _load_dotenv_if_available()
        source: dict[str, str] = dict(os.environ)
    else:
        source = {str(key): str(value) for key, value in env.items()}

    return Settings(
        telegram_bot_token=_env_text(source, "TELEGRAM_BOT_TOKEN", ""),
        rayu_api_key=_env_text(source, "RAYU_API_KEY", ""),
        rayu_base_url=_env_text(
            source, "RAYU_BASE_URL", "https://gateway.rayucode.com/v1"
        ),
        rayu_model=_env_text(source, "RAYU_MODEL", "deepseek-v4.1-flash-1017-cloud"),
        database_path=_env_text(source, "DATABASE_PATH", "jumnuybot.db"),
        cache_ttl_seconds=_env_int(source, "CACHE_TTL_SECONDS", 300),
        default_timezone=_env_text(source, "DEFAULT_TIMEZONE", "UTC"),
        reminder_lead_minutes=_env_int(source, "REMINDER_LEAD_MINUTES", 60),
        admin_user_ids=_parse_admin_user_ids(_env_text(source, "ADMIN_USER_IDS", "")),
        bot_username=_env_text(source, "BOT_USERNAME", "jumnuybot"),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

TABLE_SCHEMA: dict[str, str] = {
    "users": """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            alert_lead_minutes INTEGER NOT NULL DEFAULT 60,
            created_at TEXT NOT NULL
        )
    """,
    "groups": """
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            authorized INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,
    "messages": """
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL DEFAULT 0,
            sender_name TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )
    """,
    "assignments": """
        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            due_date TEXT,
            source_chat_id INTEGER,
            source_message_id INTEGER,
            owner_user_id INTEGER,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,
    "reminders": """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            remind_at TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """,
}

INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_messages_chat_date "
    "ON messages (chat_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_messages_date ON messages (date)",
    "CREATE INDEX IF NOT EXISTS idx_assignments_due_date "
    "ON assignments (due_date)",
    "CREATE INDEX IF NOT EXISTS idx_assignments_status ON assignments (status)",
    "CREATE INDEX IF NOT EXISTS idx_assignments_chat "
    "ON assignments (source_chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_reminders_remind_at ON reminders (remind_at)",
    "CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders (user_id)",
)

_ASSIGNMENT_UPDATABLE: frozenset[str] = frozenset(
    {
        "title",
        "due_date",
        "source_chat_id",
        "source_message_id",
        "owner_user_id",
        "status",
    }
)


def _utc_now() -> str:
    """Return the current time as a timezone-aware ISO-8601 UTC string."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class Database:
    """Small SQLite wrapper over the five-table JumnuyBot schema.

    Each instance owns its own connection, so ``Database(":memory:")`` yields a
    fully isolated database, which keeps tests fast and network-free. Schema
    creation is idempotent and also runs at construction time; callers may call
    :meth:`initialize` again safely.
    """

    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.path = path
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self.initialize()

    # -- lifecycle ---------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the live connection, raising if the database was closed."""

        if self._conn is None:
            raise RuntimeError("Database connection is closed")
        return self._conn

    def initialize(self) -> None:
        """Create tables and indexes if they do not exist (idempotent)."""

        script = ";\n".join([*TABLE_SCHEMA.values(), *INDEXES]) + ";"
        self.connection.executescript(script)
        self.connection.commit()

    def close(self) -> None:
        """Commit pending work and close the connection (idempotent)."""

        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False

    # -- low-level access --------------------------------------------------

    def execute(
        self, sql: str, params: Iterable[Any] = ()
    ) -> sqlite3.Cursor:
        """Execute a write statement and commit, returning the cursor."""

        cursor = self.connection.execute(sql, tuple(params))
        self.connection.commit()
        return cursor

    def query(
        self, sql: str, params: Iterable[Any] = ()
    ) -> list[dict[str, Any]]:
        """Execute a read statement and return plain dictionaries."""

        cursor = self.connection.execute(sql, tuple(params))
        return [dict(row) for row in cursor.fetchall()]

    # -- users -------------------------------------------------------------

    def upsert_user(
        self,
        user_id: int,
        username: str = "",
        timezone: str = "UTC",
        alert_lead_minutes: int = 60,
    ) -> None:
        """Insert or update a user profile."""

        self.execute(
            """
            INSERT INTO users (user_id, username, timezone, alert_lead_minutes, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                timezone = excluded.timezone,
                alert_lead_minutes = excluded.alert_lead_minutes
            """,
            (user_id, username, timezone, alert_lead_minutes, _utc_now()),
        )

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        """Return a user profile as a dictionary, or ``None`` when unknown."""

        rows = self.query("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return rows[0] if rows else None

    # -- groups ------------------------------------------------------------

    def upsert_group(
        self, chat_id: int, title: str = "", authorized: bool = False
    ) -> None:
        """Insert or update a group record and its authorization state."""

        now = _utc_now()
        self.execute(
            """
            INSERT INTO groups (chat_id, title, authorized, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                authorized = excluded.authorized,
                updated_at = excluded.updated_at
            """,
            (chat_id, title, int(bool(authorized)), now, now),
        )

    def set_group_authorized(self, chat_id: int, authorized: bool) -> None:
        """Set the authorization flag, creating the group row when needed."""

        now = _utc_now()
        self.execute(
            """
            INSERT INTO groups (chat_id, title, authorized, created_at, updated_at)
            VALUES (?, '', ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                authorized = excluded.authorized,
                updated_at = excluded.updated_at
            """,
            (chat_id, int(bool(authorized)), now, now),
        )

    def is_group_authorized(self, chat_id: int) -> bool:
        """Return whether a group has granted consent (``False`` if unknown)."""

        rows = self.query(
            "SELECT authorized FROM groups WHERE chat_id = ?", (chat_id,)
        )
        return bool(rows[0]["authorized"]) if rows else False

    # -- messages ----------------------------------------------------------

    def add_message(self, msg: Message) -> bool:
        """Persist a message, returning ``False`` for duplicates."""

        row = msg.to_row()
        cursor = self.execute(
            """
            INSERT OR IGNORE INTO messages
                (chat_id, message_id, user_id, sender_name, text, date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["chat_id"],
                row["message_id"],
                row["user_id"],
                row["sender_name"],
                row["text"],
                row["date"],
                _utc_now(),
            ),
        )
        return cursor.rowcount > 0

    def get_message(self, chat_id: int, message_id: int) -> Message | None:
        """Return a single stored message, or ``None`` when absent."""

        rows = self.query(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        return Message.from_row(rows[0]) if rows else None

    def list_messages(
        self, chat_id: int | None = None, since: str | None = None
    ) -> list[Message]:
        """List messages, optionally filtered by chat and ISO date lower bound."""

        clauses: list[str] = []
        params: list[Any] = []
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if since is not None:
            clauses.append("date >= ?")
            params.append(since)
        sql = "SELECT * FROM messages"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY date ASC, message_id ASC"
        return [Message.from_row(row) for row in self.query(sql, tuple(params))]

    def delete_messages_for_chat(self, chat_id: int) -> int:
        """Delete every message for a chat and return the number removed."""

        cursor = self.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        return int(cursor.rowcount)

    # -- assignments -------------------------------------------------------

    def add_assignment(
        self,
        title: str,
        due_date: str,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
        owner_user_id: int | None = None,
        status: str = "candidate",
    ) -> int:
        """Insert an assignment and return its generated identifier."""

        now = _utc_now()
        cursor = self.execute(
            """
            INSERT INTO assignments
                (title, due_date, source_chat_id, source_message_id,
                 owner_user_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                due_date,
                source_chat_id,
                source_message_id,
                owner_user_id,
                status,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid or 0)

    def get_assignment(self, id: int) -> dict[str, Any] | None:
        """Return an assignment row, or ``None`` when absent."""

        rows = self.query("SELECT * FROM assignments WHERE id = ?", (id,))
        return rows[0] if rows else None

    def list_assignments(
        self, status: str | None = None, chat_id: int | None = None
    ) -> list[dict[str, Any]]:
        """List assignments filtered by status and/or source chat."""

        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if chat_id is not None:
            clauses.append("source_chat_id = ?")
            params.append(chat_id)
        sql = "SELECT * FROM assignments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY (due_date IS NULL), due_date ASC, id ASC"
        return self.query(sql, tuple(params))

    def update_assignment(self, id: int, **fields: Any) -> bool:
        """Update whitelisted assignment fields and refresh ``updated_at``.

        Supports the JUM-103 deadline reschedule flow via ``due_date=...``.
        Unknown field names are ignored rather than interpolated into SQL.
        """

        updates = {
            key: value for key, value in fields.items() if key in _ASSIGNMENT_UPDATABLE
        }
        assignments = [f"{key} = ?" for key in updates]
        params: list[Any] = list(updates.values())
        assignments.append("updated_at = ?")
        params.append(_utc_now())
        params.append(id)
        cursor = self.execute(
            f"UPDATE assignments SET {', '.join(assignments)} WHERE id = ?",
            tuple(params),
        )
        return cursor.rowcount > 0

    def delete_assignment(self, id: int) -> bool:
        """Delete an assignment, returning whether a row was removed."""

        cursor = self.execute("DELETE FROM assignments WHERE id = ?", (id,))
        return cursor.rowcount > 0

    # -- reminders ---------------------------------------------------------

    def add_reminder(
        self, assignment_id: int, user_id: int, remind_at: str, sent: int = 0
    ) -> int:
        """Insert a reminder and return its generated identifier."""

        cursor = self.execute(
            """
            INSERT INTO reminders
                (assignment_id, user_id, remind_at, sent, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (assignment_id, user_id, remind_at, int(sent), _utc_now()),
        )
        return int(cursor.lastrowid or 0)

    def list_reminders(self, user_id: int | None = None) -> list[dict[str, Any]]:
        """List reminders ordered by time, optionally for one user."""

        if user_id is None:
            return self.query(
                "SELECT * FROM reminders ORDER BY remind_at ASC, id ASC"
            )
        return self.query(
            "SELECT * FROM reminders WHERE user_id = ? "
            "ORDER BY remind_at ASC, id ASC",
            (user_id,),
        )
