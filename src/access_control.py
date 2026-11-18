"""Administrator access and group privacy checks (SCRUM-35).

This module is the single gatekeeper for two questions asked throughout the
bot:

* may this Telegram user administer the bot (``/authorize``, ``/revoke``)?
* may questions be answered in this class group at all?

Group consent itself is recorded by :class:`~src.group_connector.GroupConnector`
and stored in the shared :class:`~src.config.Database`; this module only reads
and writes that one consent flag and never touches message content. Keeping the
checks here means every feature (search, summaries, reliability) can enforce
the same policy through one small, offline, import-light object.

The module depends only on :mod:`src.config`, so it can be imported without
network access or optional third-party packages.
"""

from __future__ import annotations

from src.config import Database, Settings

#: Reply for a caller who is not a configured administrator (authorize).
ADMIN_ONLY_AUTHORIZE = "Only an administrator can authorize this group."

#: Reply for a caller who is not a configured administrator (revoke).
ADMIN_ONLY_REVOKE = "Only an administrator can revoke this group."

#: Reply when a group has not granted consent and therefore must not be queried.
GROUP_NOT_AUTHORIZED = "This class group has not authorized Jumnuy yet."


class AccessController:
    """Decide who may administer the bot and which groups may be queried.

    Args:
        settings: Runtime settings holding the ``admin_user_ids`` allow-list.
        db: Shared storage holding each group's consent flag.
    """

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    # -- identity ----------------------------------------------------------

    def is_admin(self, user_id: int | None) -> bool:
        """Return whether ``user_id`` is a configured bot administrator.

        ``None`` (or a value that cannot be read as an integer) is never an
        administrator, which keeps anonymous or malformed Telegram updates on
        the refusing side of every admin check.
        """

        if user_id is None:
            return False
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return False
        return uid in {int(admin_id) for admin_id in self.settings.admin_user_ids}

    # -- group consent -----------------------------------------------------

    def is_authorized_group(self, chat_id: int) -> bool:
        """Return whether ``chat_id`` has granted consent to JumnuyBot.

        Unknown groups are treated as unauthorized: consent must be explicit.
        """

        return bool(self.db.is_group_authorized(int(chat_id)))

    def authorize_group(self, chat_id: int, actor_id: int | None) -> tuple[bool, str]:
        """Grant group consent on behalf of an administrator (SCRUM-35).

        Args:
            chat_id: Telegram chat identifier of the class group.
            actor_id: Telegram user id of the person issuing ``/authorize``.

        Returns:
            ``(True, "Group authorized.")`` when an administrator granted
            consent, otherwise ``(False, message)`` with the group state left
            unchanged.
        """

        if not self.is_admin(actor_id):
            return False, ADMIN_ONLY_AUTHORIZE

        self.db.set_group_authorized(int(chat_id), True)
        return True, "Group authorized."

    def revoke_group(self, chat_id: int, actor_id: int | None) -> tuple[bool, str]:
        """Withdraw group consent on behalf of an administrator (SCRUM-35).

        Revoking only stops answering; stored messages are intentionally kept.

        Args:
            chat_id: Telegram chat identifier of the class group.
            actor_id: Telegram user id of the person issuing ``/revoke``.

        Returns:
            ``(True, "Group access revoked.")`` when an administrator revoked
            consent, otherwise ``(False, message)``.
        """

        if not self.is_admin(actor_id):
            return False, ADMIN_ONLY_REVOKE

        self.db.set_group_authorized(int(chat_id), False)
        return True, "Group access revoked."

    # -- query gate --------------------------------------------------------

    def check_query(self, user_id: int | None, chat_id: int) -> tuple[bool, str]:
        """Decide whether a question may be answered in a group (SCRUM-35).

        Consent is per group, not per student, so any member of an authorized
        group is allowed regardless of admin status. ``user_id`` may be
        ``None`` for anonymous members; it is accepted but never grants access
        to a group that has not authorized the bot.

        Args:
            user_id: Telegram user id of the asker, or ``None`` when unknown.
            chat_id: Telegram chat identifier the question was sent in.

        Returns:
            ``(True, "")`` when the group is authorized, otherwise
            ``(False, message)`` explaining that consent is missing.
        """

        if not self.is_authorized_group(chat_id):
            return False, GROUP_NOT_AUTHORIZED
        return True, ""
