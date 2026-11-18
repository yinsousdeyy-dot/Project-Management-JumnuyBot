"""Tests for SCRUM-33 keyword + intent search and the TTL cache."""

from __future__ import annotations

from typing import Any

from src.config import Database
from src.message_search import MessageSearch, TTLCache
from src.models import Message


class FakeClock:
    """Deterministic monotonic clock for TTL tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingDB:
    """Duck-typed database that records how often messages are loaded."""

    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages
        self.calls = 0

    def list_messages(
        self, chat_id: int | None = None, since: str | None = None
    ) -> list[Message]:
        self.calls += 1
        return [
            message
            for message in self.messages
            if chat_id is None or message.chat_id == chat_id
        ]


def _seed(db: Database) -> None:
    """Insert a small deterministic message corpus."""

    db.add_message(
        Message(
            chat_id=1,
            message_id=1,
            text="Midterm exam for CS101 is next Friday",
            date="2026-11-01T09:00:00+00:00",
        )
    )
    db.add_message(
        Message(
            chat_id=1,
            message_id=2,
            text="The assignment due date is moved to Monday",
            date="2026-11-02T09:00:00+00:00",
        )
    )
    db.add_message(
        Message(
            chat_id=1,
            message_id=3,
            text="Lunch plans at the canteen",
            date="2026-11-03T09:00:00+00:00",
        )
    )
    db.add_message(
        Message(
            chat_id=2,
            message_id=4,
            text="Quiz reminder: bring your student card",
            date="2026-11-04T09:00:00+00:00",
        )
    )


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------


def test_ttl_cache_get_set_len_and_clear() -> None:
    clock = FakeClock()
    cache: TTLCache = TTLCache(ttl_seconds=10, clock=clock)

    cache.set("a", 1)
    cache.set("ab", 2)
    cache.set("b", 3)

    assert cache.get("a") == 1
    assert cache.get("missing") is None
    assert len(cache) == 3

    assert cache.invalidate("a") == 2
    assert cache.get("a") is None
    assert cache.get("ab") is None
    assert cache.get("b") == 3

    assert cache.invalidate() == 1
    assert len(cache) == 0

    cache.set("a", 1)
    cache.clear()
    assert len(cache) == 0


def test_ttl_cache_expires_lazily() -> None:
    clock = FakeClock()
    cache: TTLCache = TTLCache(ttl_seconds=5, clock=clock)

    cache.set("key", "value")
    clock.advance(4)
    assert cache.get("key") == "value"
    clock.advance(2)
    assert cache.get("key") is None
    assert len(cache) == 0


def test_ttl_cache_invalidate_ignores_expired_entries() -> None:
    clock = FakeClock()
    cache: TTLCache = TTLCache(ttl_seconds=5, clock=clock)

    cache.set("search:1:x:10", "old")
    clock.advance(10)
    # The only matching entry has already expired, so nothing is removed.
    assert cache.invalidate("search:1:") == 0

    cache.set("search:1:x:10", "fresh")
    clock.advance(1)
    assert cache.invalidate("search:1:") == 1
    assert cache.get("search:1:x:10") is None


# ---------------------------------------------------------------------------
# Ranking and matching
# ---------------------------------------------------------------------------


def test_relevant_message_ranks_first(db: Database) -> None:
    _seed(db)
    search = MessageSearch(db)

    hits = search.search("exam midterm")

    assert hits
    assert hits[0].message.message_id == 1
    assert hits[0].score > 0
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


def test_synonym_expansion_finds_due_date_for_deadline(db: Database) -> None:
    _seed(db)
    search = MessageSearch(db)

    hits = search.search("deadline")

    assert hits
    assert hits[0].message.message_id == 2


def test_no_match_returns_empty(db: Database) -> None:
    _seed(db)
    search = MessageSearch(db)

    assert search.search("volleyball") == []


def test_empty_and_whitespace_queries_return_empty_without_caching(
    db: Database,
) -> None:
    _seed(db)
    counter = CountingDB([])
    search = MessageSearch(counter)

    assert search.search("") == []
    assert search.search("   ") == []
    assert search.search("!!!") == []
    assert counter.calls == 0
    assert len(search.cache) == 0


def test_limit_is_respected(db: Database) -> None:
    _seed(db)
    search = MessageSearch(db)

    hits = search.search("the", limit=2)

    assert len(hits) == 2
    assert search.search("the", limit=0) == []


def test_chat_id_filter_is_respected(db: Database) -> None:
    _seed(db)
    search = MessageSearch(db)

    chat_two = search.search("student", chat_id=2)
    assert [hit.message.message_id for hit in chat_two] == [4]

    chat_one = search.search("student", chat_id=1)
    assert chat_one == []

    everywhere = search.search("student")
    assert [hit.message.message_id for hit in everywhere] == [4]


def test_recency_breaks_ties_towards_newer_message(db: Database) -> None:
    db.add_message(
        Message(
            chat_id=1,
            message_id=10,
            text="exam notice",
            date="2026-11-01T00:00:00+00:00",
        )
    )
    db.add_message(
        Message(
            chat_id=1,
            message_id=11,
            text="exam notice",
            date="2026-11-10T00:00:00+00:00",
        )
    )
    search = MessageSearch(db)

    hits = search.search("exam")

    assert [hit.message.message_id for hit in hits] == [11, 10]
    assert hits[0].score > hits[1].score


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_cache_serves_second_call_without_relisting(
    db: Database,
) -> None:
    _seed(db)
    counter = CountingDB(db.list_messages())
    search = MessageSearch(counter)

    first = search.search("exam")
    assert counter.calls == 1
    second = search.search("exam")
    assert counter.calls == 1
    assert [hit.message.message_id for hit in first] == [
        hit.message.message_id for hit in second
    ]

    # A different limit is a distinct cache key and must re-query.
    search.search("exam", limit=1)
    assert counter.calls == 2


def test_cache_expires_with_fake_clock() -> None:
    clock = FakeClock()
    messages = [
        Message(
            chat_id=1,
            message_id=1,
            text="exam on Monday",
            date="2026-11-01T00:00:00+00:00",
        )
    ]
    counter = CountingDB(messages)
    search = MessageSearch(counter, cache=TTLCache(ttl_seconds=5, clock=clock))

    search.search("exam")
    search.search("exam")
    assert counter.calls == 1

    clock.advance(6)
    search.search("exam")
    assert counter.calls == 2


def test_invalidate_cache_per_chat_and_all() -> None:
    messages = [
        Message(
            chat_id=1,
            message_id=1,
            text="exam one",
            date="2026-11-01T00:00:00+00:00",
        ),
        Message(
            chat_id=2,
            message_id=2,
            text="exam two",
            date="2026-11-02T00:00:00+00:00",
        ),
    ]
    counter = CountingDB(messages)
    search = MessageSearch(counter)

    search.search("exam", chat_id=1)
    search.search("exam", chat_id=2)
    assert counter.calls == 2

    # Populate a chat-scoped key too, to prove it is invalidated as well.
    search.cache.set("chat:1", {"stale": True})

    assert search.invalidate_cache(1) == 2  # search:1 + chat:1

    search.search("exam", chat_id=1)
    assert counter.calls == 3  # recomputed
    search.search("exam", chat_id=2)
    assert counter.calls == 3  # still cached for chat 2

    # Both the recomputed chat-1 entry and the chat-2 entry are now cached.
    assert search.invalidate_cache() == 2
    search.search("exam", chat_id=2)
    assert counter.calls == 4
