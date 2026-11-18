"""Telegram group consent flow (SCRUM-31).

JumnuyBot only stores and answers questions about a Telegram group after an
administrator has explicitly granted consent with ``/authorize``. This module
implements that handshake on top of the shared :class:`~src.config.Database`
and :class:`~src.config.Settings` contracts:

* being added to a group never records messages by itself;
* a configured admin user id or one of the group's own Telegram
  administrators may authorize or revoke a group;
* revoking consent disables answering but never deletes stored messages.

All methods are offline and synchronous, so they are safe to drive directly
from tests or from a thin Telegram handler layer.
"""

from __future__ import annotations

from src.config import Database, Settings

#: Status returned by :meth:`GroupConnector.on_bot_added` for a new group.
STATUS_CONSENT_REQUIRED = "consent_required"

#: Status returned by :meth:`GroupConnector.on_bot_added` for an approved group.
STATUS_AUTHORIZED = "authorized"

#: Message shown when a group has not yet granted consent (Khmer + English).
CONSENT_PROMPT = (
    "សូមអរគុណដែលបានបន្ថែម JumnuyBot ចូលក្នុងក្រុម "
    "(Thanks for adding JumnuyBot). "
    "ដើម្បីការពារភាពឯកជន បូតនឹងមិនរក្សាទុក ឬឆ្លើយសំណួរក្នុងក្រុមនេះទេ "
    "រហូតដល់អ្នកគ្រប់គ្រង (admin) ផ្ញើ /authorize ដើម្បីយល់ព្រម "
    "(An admin must send /authorize to grant consent). "
    "ម្ចាស់ក្រុមអាចដកការអនុញ្ញាតបានគ្រប់ពេលដោយ /revoke "
    "(The group owner can revoke this at any time with /revoke)."
)


class GroupConnector:
    """Handle bot-add, authorize and revoke events for Telegram groups.

    Args:
        db: Shared storage used to persist the group consent flag.
        settings: Runtime settings providing the admin user id allow-list.
    """

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # -- helpers -----------------------------------------------------------

    def _is_admin(self, user_id: int) -> bool:
        """Return whether ``user_id`` is configured as a bot administrator."""

        return int(user_id) in {int(uid) for uid in self.settings.admin_user_ids}

    def _group_exists(self, chat_id: int) -> bool:
        """Return whether a group row has already been recorded."""

        rows = self.db.query(
            "SELECT 1 FROM groups WHERE chat_id = ? LIMIT 1", (int(chat_id),)
        )
        return bool(rows)

    def consent_prompt(self, chat_id: int) -> str:
        """Return the consent request text shown after the bot is added.

        Args:
            chat_id: Telegram chat identifier the prompt is addressed to.
        """

        return f"[chat {int(chat_id)}] {CONSENT_PROMPT}"

    # -- events ------------------------------------------------------------

    def on_bot_added(
        self, chat_id: int, title: str, actor_id: int
    ) -> dict[str, object]:
        """Record a newly added group and request consent.

        A brand-new group is stored with ``authorized=False``. Adding the bot
        again to an already authorized group is a no-op so that existing
        consent and stored messages are preserved.

        Args:
            chat_id: Telegram chat identifier of the group.
            title: Human-readable group title from Telegram.
            actor_id: Telegram user id of whoever added the bot.

        Returns:
            ``{"status": "consent_required", "message": str}`` for a new group,
            or ``{"status": "authorized", "message": str}`` when the group was
            already authorized.
        """

        if self.db.is_group_authorized(chat_id):
            return {
                "status": STATUS_AUTHORIZED,
                "message": (
                    "This group has already granted consent; "
                    "the bot is ready to answer questions."
                ),
            }

        self.db.upsert_group(int(chat_id), str(title), authorized=False)
        return {
            "status": STATUS_CONSENT_REQUIRED,
            "message": self.consent_prompt(chat_id),
        }

    def on_authorize(
        self,
        chat_id: int,
        actor_id: int,
        *,
        actor_is_group_admin: bool = False,
    ) -> dict[str, object]:
        """Grant consent for a group when requested by an administrator.

        Args:
            chat_id: Telegram chat identifier of the group.
            actor_id: Telegram user id of the requester.
            actor_is_group_admin: Whether Telegram reports the requester as a
                creator or administrator of this group.

        Returns:
            ``{"ok": True, "message": str}`` on success. An actor who is
            neither a configured admin nor a group administrator, or a group
            that was never registered, yields ``{"ok": False, ...}``; an
            unknown group must be added through the bot first so that consent
            is always tied to a real chat record.
        """

        if not self._is_admin(actor_id) and not actor_is_group_admin:
            return {
                "ok": False,
                "message": (
                    "Only a group administrator can authorize this group. "
                    "(មានតែអ្នកគ្រប់គ្រងក្រុម ឬអ្នកគ្រប់គ្រងបូតទេ "
                    "ទើបអាចអនុញ្ញាតក្រុមនេះបាន។)"
                ),
            }

        if not self._group_exists(chat_id):
            return {
                "ok": False,
                "message": (
                    "This group is not registered yet. Add JumnuyBot to the "
                    "group first, then send /authorize."
                ),
            }

        self.db.set_group_authorized(int(chat_id), True)
        return {
            "ok": True,
            "message": (
                "Consent granted. JumnuyBot may now store and answer "
                "questions in this group."
            ),
        }

    def on_revoke(
        self,
        chat_id: int,
        actor_id: int,
        *,
        actor_is_group_admin: bool = False,
    ) -> dict[str, object]:
        """Revoke consent for a group without deleting stored messages.

        Args:
            chat_id: Telegram chat identifier of the group.
            actor_id: Telegram user id of the requester.
            actor_is_group_admin: Whether Telegram reports the requester as a
                creator or administrator of this group.

        Returns:
            ``{"ok": True, "message": str}`` on success, otherwise
            ``{"ok": False, ...}``. Messages are intentionally left intact.
        """

        if not self._is_admin(actor_id) and not actor_is_group_admin:
            return {
                "ok": False,
                "message": (
                    "Only a group administrator can revoke consent for this "
                    "group. (មានតែអ្នកគ្រប់គ្រងក្រុម ឬអ្នកគ្រប់គ្រងបូតទេ "
                    "ទើបអាចដកសិទ្ធិក្រុមនេះបាន។)"
                ),
            }

        if not self._group_exists(chat_id):
            return {
                "ok": False,
                "message": "This group is not registered, nothing to revoke.",
            }

        self.db.set_group_authorized(int(chat_id), False)
        return {
            "ok": True,
            "message": (
                "Consent revoked. The bot will stop answering in this group; "
                "existing messages are kept."
            ),
        }
