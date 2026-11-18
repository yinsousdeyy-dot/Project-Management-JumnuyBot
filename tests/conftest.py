"""Shared pytest fixtures for Sprint 3 tests (SCRUM-38).

All fixtures are deterministic and offline: the database is in-memory and the
settings contain obvious test placeholders, never real credentials.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.config import Database, Settings


@pytest.fixture()
def db() -> Iterator[Database]:
    """Provide an initialized in-memory database, closed after the test."""

    database = Database(":memory:")
    database.initialize()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture()
def settings() -> Settings:
    """Provide deterministic test settings with one admin user."""

    return Settings(
        telegram_bot_token="test-telegram-token",
        rayu_api_key="test-rayu-key",
        rayu_base_url="https://rayu.example.invalid/v1",
        rayu_model="test-model",
        database_path=":memory:",
        cache_ttl_seconds=60,
        default_timezone="Asia/Bangkok",
        reminder_lead_minutes=30,
        admin_user_ids=(1,),
        bot_username="testbot",
    )
