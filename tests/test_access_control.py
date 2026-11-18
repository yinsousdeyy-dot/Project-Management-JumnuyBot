"""Tests for administrator and privacy checks (SCRUM-35).

Every test runs against the shared in-memory ``db`` and deterministic
``settings`` fixtures from ``tests/conftest.py``. No network access is used.
"""

from __future__ import annotations

import pytest

from src.access_control import AccessController
from src.config import Database, Settings

ADMIN_ID = 1
NON_ADMIN_ID = 42
CHAT_ID = -1001234567890
UNKNOWN_CHAT_ID = -1009876543210


@pytest.fixture()
def access(settings: Settings, db: Database) -> AccessController:
    """Provide an access controller wired to the test settings and database."""

    return AccessController(settings, db)


# -- admin identity --------------------------------------------------------


def test_configured_admin_is_recognized(access: AccessController) -> None:
    """A user id from ``settings.admin_user_ids`` is an administrator."""

    assert access.is_admin(ADMIN_ID) is True


def test_non_admin_is_not_recognized(access: AccessController) -> None:
    """A user id outside the allow-list is not an administrator."""

    assert access.is_admin(NON_ADMIN_ID) is False


def test_none_user_is_never_admin(access: AccessController) -> None:
    """Anonymous or missing user ids never pass an admin check."""

    assert access.is_admin(None) is False


# -- authorize / revoke ----------------------------------------------------


def test_admin_can_authorize_group(access: AccessController, db: Database) -> None:
    """An administrator flips the group consent flag on."""

    ok, message = access.authorize_group(CHAT_ID, ADMIN_ID)

    assert ok is True
    assert message == "Group authorized."
    assert db.is_group_authorized(CHAT_ID) is True
    assert access.is_authorized_group(CHAT_ID) is True


def test_non_admin_cannot_authorize_group(
    access: AccessController, db: Database
) -> None:
    """A non-admin refusal leaves the group unauthorized."""

    ok, message = access.authorize_group(CHAT_ID, NON_ADMIN_ID)

    assert ok is False
    assert message == "Only an administrator can authorize this group."
    assert db.is_group_authorized(CHAT_ID) is False


def test_none_actor_cannot_authorize_group(
    access: AccessController, db: Database
) -> None:
    """A missing actor id cannot authorize a group."""

    ok, message = access.authorize_group(CHAT_ID, None)

    assert ok is False
    assert message == "Only an administrator can authorize this group."
    assert db.is_group_authorized(CHAT_ID) is False


def test_admin_can_revoke_group(access: AccessController, db: Database) -> None:
    """An administrator flips a previously authorized group back off."""

    assert access.authorize_group(CHAT_ID, ADMIN_ID)[0] is True
    ok, message = access.revoke_group(CHAT_ID, ADMIN_ID)

    assert ok is True
    assert message == "Group access revoked."
    assert db.is_group_authorized(CHAT_ID) is False
    assert access.is_authorized_group(CHAT_ID) is False


def test_non_admin_cannot_revoke_group(
    access: AccessController, db: Database
) -> None:
    """A non-admin cannot revoke consent from an authorized group."""

    assert access.authorize_group(CHAT_ID, ADMIN_ID)[0] is True

    ok, message = access.revoke_group(CHAT_ID, NON_ADMIN_ID)

    assert ok is False
    assert message == "Only an administrator can revoke this group."
    assert db.is_group_authorized(CHAT_ID) is True


# -- query gate ------------------------------------------------------------


def test_query_refused_before_authorization(access: AccessController) -> None:
    """Questions in a group without consent are refused."""

    ok, message = access.check_query(NON_ADMIN_ID, CHAT_ID)

    assert ok is False
    assert message == "This class group has not authorized Jumnuy yet."


def test_query_allowed_after_authorization(access: AccessController) -> None:
    """Any member may ask once the group has granted consent."""

    assert access.authorize_group(CHAT_ID, ADMIN_ID)[0] is True

    ok, message = access.check_query(NON_ADMIN_ID, CHAT_ID)

    assert ok is True
    assert message == ""


def test_query_refused_again_after_revoke(access: AccessController) -> None:
    """Revoking consent immediately stops further questions in the group."""

    assert access.authorize_group(CHAT_ID, ADMIN_ID)[0] is True
    assert access.revoke_group(CHAT_ID, ADMIN_ID)[0] is True

    ok, message = access.check_query(NON_ADMIN_ID, CHAT_ID)

    assert ok is False
    assert message == "This class group has not authorized Jumnuy yet."


def test_query_unknown_group_is_refused(access: AccessController) -> None:
    """A group with no stored consent record is treated as unauthorized."""

    assert access.is_authorized_group(UNKNOWN_CHAT_ID) is False

    ok, message = access.check_query(ADMIN_ID, UNKNOWN_CHAT_ID)

    assert ok is False
    assert message == "This class group has not authorized Jumnuy yet."


def test_none_user_allowed_in_authorized_group(access: AccessController) -> None:
    """Anonymous members are allowed through once the group has consented."""

    assert access.authorize_group(CHAT_ID, ADMIN_ID)[0] is True

    assert access.check_query(None, CHAT_ID) == (True, "")


def test_none_user_refused_in_unauthorized_group(access: AccessController) -> None:
    """Anonymous members gain nothing in a group without consent."""

    ok, _ = access.check_query(None, CHAT_ID)

    assert ok is False
