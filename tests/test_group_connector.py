"""Tests for the Telegram group consent flow (SCRUM-31).

Every test uses the shared in-memory ``db`` and deterministic ``settings``
fixtures from ``tests/conftest.py``; no network access is performed.
"""

from __future__ import annotations

import pytest

from src.config import Database, Settings
from src.group_connector import CONSENT_PROMPT, GroupConnector
from src.models import Message

CHAT_ID = -1001234567890
OTHER_CHAT_ID = -1009876543210
ADMIN_ID = 1
NON_ADMIN_ID = 42

THAI_RANGE = (0x0E00, 0x0E7F)
KHMER_RANGE = (0x1780, 0x17FF)


def _has_chars_in_range(text: str, bounds: tuple[int, int]) -> bool:
    """Return whether ``text`` contains any code point within ``bounds``."""

    low, high = bounds
    return any(low <= ord(char) <= high for char in text)


@pytest.fixture()
def connector(db: Database, settings: Settings) -> GroupConnector:
    """Provide a connector wired to the in-memory database and test settings."""

    return GroupConnector(db, settings)


def test_added_group_is_stored_unauthorized(connector: GroupConnector, db: Database) -> None:
    """Adding the bot records the group but never auto-authorizes it."""

    result = connector.on_bot_added(CHAT_ID, "Class 3A", ADMIN_ID)

    assert result["status"] == "consent_required"
    assert isinstance(result["message"], str)
    assert "/authorize" in result["message"]
    assert db.is_group_authorized(CHAT_ID) is False


def test_admin_authorize_enables_group(connector: GroupConnector, db: Database) -> None:
    """A configured admin can flip the group to authorized."""

    connector.on_bot_added(CHAT_ID, "Class 3A", ADMIN_ID)

    result = connector.on_authorize(CHAT_ID, ADMIN_ID)

    assert result["ok"] is True
    assert db.is_group_authorized(CHAT_ID) is True


def test_non_admin_authorize_is_refused(connector: GroupConnector, db: Database) -> None:
    """A non-admin actor cannot authorize a group."""

    connector.on_bot_added(CHAT_ID, "Class 3A", ADMIN_ID)

    result = connector.on_authorize(CHAT_ID, NON_ADMIN_ID)

    assert result["ok"] is False
    assert isinstance(result["message"], str)
    assert db.is_group_authorized(CHAT_ID) is False


def test_revoke_disables_access_but_keeps_messages(
    connector: GroupConnector, db: Database
) -> None:
    """Revoking consent stops access without deleting stored messages."""

    connector.on_bot_added(CHAT_ID, "Class 3A", ADMIN_ID)
    connector.on_authorize(CHAT_ID, ADMIN_ID)
    message = Message(
        chat_id=CHAT_ID,
        message_id=7,
        text="Homework due Friday",
        date="2026-11-18T09:00:00+07:00",
        user_id=NON_ADMIN_ID,
        sender_name="Student",
    )
    assert db.add_message(message) is True

    result = connector.on_revoke(CHAT_ID, ADMIN_ID)

    assert result["ok"] is True
    assert db.is_group_authorized(CHAT_ID) is False
    stored = db.get_message(CHAT_ID, 7)
    assert stored is not None
    assert stored.text == "Homework due Friday"


def test_non_admin_revoke_is_refused(connector: GroupConnector, db: Database) -> None:
    """A non-admin actor cannot revoke a group's consent."""

    connector.on_bot_added(CHAT_ID, "Class 3A", ADMIN_ID)
    connector.on_authorize(CHAT_ID, ADMIN_ID)

    result = connector.on_revoke(CHAT_ID, NON_ADMIN_ID)

    assert result["ok"] is False
    assert db.is_group_authorized(CHAT_ID) is True


def test_readding_authorized_group_preserves_authorization(
    connector: GroupConnector, db: Database
) -> None:
    """Re-adding the bot to an authorized group must not reset consent."""

    connector.on_bot_added(CHAT_ID, "Class 3A", ADMIN_ID)
    connector.on_authorize(CHAT_ID, ADMIN_ID)

    result = connector.on_bot_added(CHAT_ID, "Class 3A renamed", ADMIN_ID)

    assert result["status"] == "authorized"
    assert db.is_group_authorized(CHAT_ID) is True


def test_authorize_unknown_group_is_refused(connector: GroupConnector, db: Database) -> None:
    """Authorizing a group that was never added returns a clear refusal."""

    result = connector.on_authorize(OTHER_CHAT_ID, ADMIN_ID)

    assert result["ok"] is False
    assert isinstance(result["message"], str)
    assert db.is_group_authorized(OTHER_CHAT_ID) is False


def test_revoke_unknown_group_is_refused(connector: GroupConnector, db: Database) -> None:
    """Revoking a group that was never added returns a clear refusal."""

    result = connector.on_revoke(OTHER_CHAT_ID, ADMIN_ID)

    assert result["ok"] is False
    assert "not registered" in str(result["message"])


def test_consent_prompt_mentions_authorize_and_chat(
    connector: GroupConnector,
) -> None:
    """The consent helper returns a usable prompt for the given chat."""

    prompt = connector.consent_prompt(CHAT_ID)

    assert "/authorize" in prompt
    assert str(CHAT_ID) in prompt
    # The bilingual prompt mentions /revoke once in Khmer and once in the
    # English parenthetical, so the sentence itself must appear exactly once.
    assert prompt.count("The group owner can revoke this at any time") == 1
    assert prompt.count("(The group owner can revoke this at any time with /revoke).") == 1


# -- localization: Khmer + English only, no Thai ---------------------------


def test_consent_prompt_has_no_thai_characters() -> None:
    """The consent prompt must not contain Thai code points (U+0E00–U+0E7F)."""

    assert _has_chars_in_range(CONSENT_PROMPT, THAI_RANGE) is False


def test_consent_prompt_contains_khmer_characters() -> None:
    """The consent prompt must contain Khmer code points (U+1780–U+17FF)."""

    assert _has_chars_in_range(CONSENT_PROMPT, KHMER_RANGE) is True


def test_consent_prompt_keeps_command_hints() -> None:
    """The consent prompt must still tell users about /authorize and /revoke."""

    assert "/authorize" in CONSENT_PROMPT
    assert "/revoke" in CONSENT_PROMPT


def test_non_admin_refusals_have_no_thai_characters(
    connector: GroupConnector, db: Database
) -> None:
    """Both non-admin refusal messages must be free of Thai code points."""

    connector.on_bot_added(CHAT_ID, "Class 3A", ADMIN_ID)

    authorize = connector.on_authorize(CHAT_ID, NON_ADMIN_ID)
    revoke = connector.on_revoke(CHAT_ID, NON_ADMIN_ID)

    assert authorize["ok"] is False
    assert revoke["ok"] is False
    assert _has_chars_in_range(str(authorize["message"]), THAI_RANGE) is False
    assert _has_chars_in_range(str(revoke["message"]), THAI_RANGE) is False
