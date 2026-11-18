"""Composition root for the live Telegram bot (SCRUM-45).

:class:`BotServices` bundles the already-complete Sprint 3 feature modules --
consent, ingestion, search, summarization and access control -- behind one
small object that the Telegram handler layer reads from
``context.bot_data["services"]``.

Two invariants make this module safe in production and in tests:

* **No token, no network.** :func:`build_services` only constructs objects.
  Creating :class:`~src.announcement_summary.AnnouncementSummarizer` never
  contacts the Rayu gateway, and nothing here talks to Telegram.
* **One serialized database connection.** The shared
  :class:`~src.config.Database` owns a single SQLite connection created with
  ``check_same_thread=False``. :meth:`BotServices.call` funnels every blocking
  operation through :func:`asyncio.to_thread` while holding one
  :class:`threading.Lock`, so concurrent handlers never interleave writes on
  that connection and the event loop itself is never blocked.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.access_control import AccessController
from src.announcement_summary import AnnouncementSummarizer
from src.config import Database, Settings, load_settings
from src.group_connector import GroupConnector
from src.message_ingestion import MessageIngestor
from src.message_search import MessageSearch, TTLCache

__all__ = ["BotServices", "build_services"]


@dataclass
class BotServices:
    """Every Sprint 3 feature module, ready for the Telegram handler layer.

    Attributes:
        settings: Immutable runtime configuration.
        db: Shared SQLite storage (one connection, ``check_same_thread=False``).
        cache: TTL cache shared by search and ingestion so a newly stored
            message invalidates cached search results.
        search: Keyword/intent message search.
        ingestor: Telegram message ingestion with privacy and dedupe rules.
        summarizer: Announcement summarizer used by ``/today``.
        groups: Group consent connector used by add/authorize/revoke flows.
        access: Admin and group-privacy gatekeeper.
        lock: Serializes blocking work submitted through :meth:`call`.
    """

    settings: Settings
    db: Database
    cache: TTLCache
    search: MessageSearch
    ingestor: MessageIngestor
    summarizer: AnnouncementSummarizer
    groups: GroupConnector
    access: AccessController
    lock: threading.Lock = field(default_factory=threading.Lock)

    async def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run blocking ``fn(*args, **kwargs)`` in a worker thread under a lock.

        The lock is acquired in a worker thread as well, so awaiting another
        coroutine never blocks the event loop while this call is in flight.
        Every module touching the shared SQLite connection should go through
        this method.

        Args:
            fn: Synchronous callable, typically a feature-module method.
            *args: Positional arguments forwarded to ``fn``.
            **kwargs: Keyword arguments forwarded to ``fn``.

        Returns:
            Whatever ``fn`` returns, propagated unchanged.
        """

        await asyncio.to_thread(self.lock.acquire)
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            self.lock.release()


def build_services(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    summarizer_client: Any | None = None,
) -> BotServices:
    """Assemble :class:`BotServices` without requiring a token or network.

    Args:
        settings: Optional pre-built settings. Loaded from the environment
            when omitted; an empty ``TELEGRAM_BOT_TOKEN`` is perfectly valid.
        db: Optional storage handle. A fresh :class:`~src.config.Database` for
            ``settings.database_path`` is created (and initialized) when
            omitted, which keeps tests on ``Database(":memory:")``.
        summarizer_client: Optional pre-built AI client exposing
            ``complete(prompt) -> str``. Used by tests to stay offline; when
            omitted the summarizer decides based on ``settings.rayu_api_key``.

    Returns:
        A fully wired :class:`BotServices` instance. No Telegram or Rayu
        request is made by this function.
    """

    resolved = settings or load_settings()
    database = db if db is not None else Database(resolved.database_path)
    database.initialize()

    # One cache instance is shared so ingestion invalidates search results.
    cache = TTLCache(resolved.cache_ttl_seconds)
    search = MessageSearch(database, cache, resolved.cache_ttl_seconds)
    ingestor = MessageIngestor(database, cache=cache)
    summarizer = AnnouncementSummarizer(resolved, database, summarizer_client)
    groups = GroupConnector(database, resolved)
    access = AccessController(resolved, database)

    return BotServices(
        settings=resolved,
        db=database,
        cache=cache,
        search=search,
        ingestor=ingestor,
        summarizer=summarizer,
        groups=groups,
        access=access,
    )
