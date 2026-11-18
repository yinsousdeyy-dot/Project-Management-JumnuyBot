"""Keyword and intent search with a TTL cache (SCRUM-33).

This module provides the student-facing search feature: :class:`MessageSearch`
ranks stored Telegram messages against a free-text query, expands common
intent synonyms (deadline/due, exam/quiz, ...), and caches results so repeated
queries do not re-score the whole corpus. :class:`TTLCache` is a tiny
monotonic-clock cache shared here and reused by the reliability module.

The implementation is fully offline and deterministic: scoring never calls
``datetime.now()``; recency is derived only from the stored message dates.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from src.config import Database
from src.models import Message, SearchHit

# ---------------------------------------------------------------------------
# Intent synonyms
# ---------------------------------------------------------------------------

# Each group is a set of interchangeable intents. Tokens may appear in more
# than one group (e.g. "assignment" belongs to both the deadline and the task
# vocabulary); the maps below merge every group a token belongs to.
_INTENT_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"deadline", "due", "submit", "assignment"}),
    frozenset({"exam", "quiz", "test", "midterm", "final"}),
    frozenset({"announce", "announcement", "notice", "lecture"}),
    frozenset({"task", "homework", "assignment"}),
)

_TOKEN_SYNONYMS: dict[str, frozenset[str]] = {}
for _group in _INTENT_GROUPS:
    for _token in _group:
        _TOKEN_SYNONYMS[_token] = _TOKEN_SYNONYMS.get(_token, frozenset()) | _group

_WORD_RE = re.compile(r"[a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")


def tokenize(text: str) -> list[str]:
    """Return lowercase alphanumeric words from ``text``.

    Args:
        text: Arbitrary message or query text.

    Returns:
        A list of lowercase tokens; punctuation is discarded.
    """

    return _WORD_RE.findall(text.lower())


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------


class TTLCache:
    """A minimal lazily-expiring in-memory cache keyed by string.

    Entries expire ``ttl_seconds`` after they were written. Expiry is checked
    lazily on access, so no background timer or thread is required. The clock
    is injectable to keep tests deterministic.

    Args:
        ttl_seconds: Lifetime of an entry in seconds. Non-positive values
            make entries expire immediately.
        clock: Monotonic time source returning seconds as a float. Defaults to
            :func:`time.monotonic`.
    """

    def __init__(
        self,
        ttl_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> object | None:
        """Return the cached value for ``key`` or ``None`` when absent/expired."""

        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self._clock() >= expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: object) -> None:
        """Store ``value`` under ``key`` with the configured TTL."""

        self._store[key] = (self._clock() + self.ttl_seconds, value)

    def invalidate(self, prefix: str = "") -> int:
        """Remove entries whose key starts with ``prefix``.

        Args:
            prefix: Key prefix to remove. An empty prefix removes every
                (non-expired) entry.

        Returns:
            The number of entries removed.
        """

        self._purge_expired()
        if not prefix:
            removed = len(self._store)
            self._store.clear()
            return removed
        keys = [key for key in self._store if key.startswith(prefix)]
        for key in keys:
            del self._store[key]
        return len(keys)

    def clear(self) -> None:
        """Remove every entry, expired or not."""

        self._store.clear()

    def __len__(self) -> int:
        """Return the number of live (non-expired) entries."""

        self._purge_expired()
        return len(self._store)

    def _purge_expired(self) -> None:
        """Drop entries whose TTL has elapsed."""

        now = self._clock()
        expired = [
            key for key, (expires_at, _) in self._store.items() if now >= expires_at
        ]
        for key in expired:
            del self._store[key]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _parse_timestamp(value: str) -> float | None:
    """Parse an ISO-8601 timestamp into epoch seconds, or ``None`` if invalid.

    Naive timestamps are interpreted as UTC so ordering stays deterministic
    across machines.
    """

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _query_concepts(tokens: Iterable[str]) -> list[frozenset[str]]:
    """Map query tokens to their expanded intent-concept sets.

    Each distinct raw query token contributes exactly one concept (itself plus
    any known synonyms), preserving the "per query-token" scoring weight.
    """

    concepts: list[frozenset[str]] = []
    for token in dict.fromkeys(tokens):
        concepts.append(_TOKEN_SYNONYMS.get(token, frozenset({token})))
    return concepts


def _normalize_text(text: str) -> str:
    """Collapse whitespace and lowercase ``text`` for phrase matching."""

    return _WHITESPACE_RE.sub(" ", text.lower()).strip()


class MessageSearch:
    """Keyword + intent search over stored messages, backed by a TTL cache.

    Args:
        db: Storage handle exposing ``list_messages(chat_id)`` (a
            :class:`~src.config.Database` in production).
        cache: Optional shared cache. A new :class:`TTLCache` is created when
            omitted.
        ttl_seconds: TTL used for the internally created cache.
    """

    SCORE_PER_CONCEPT = 2.0
    SYNONYM_WEIGHT = 0.75
    MAX_PHRASE_BONUS = 2.0
    MAX_OVERLAP_BONUS = 0.5
    MAX_RECENCY_BONUS = 1.0

    def __init__(
        self,
        db: Database,
        cache: TTLCache | None = None,
        ttl_seconds: int = 300,
    ) -> None:
        self.db = db
        self.cache = cache if cache is not None else TTLCache(ttl_seconds)
        self.ttl_seconds = ttl_seconds

    # -- public API --------------------------------------------------------

    def search(
        self,
        query: str,
        chat_id: int | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        """Search stored messages and return the best matches first.

        The query is tokenized and expanded with intent synonyms; each message
        is scored by matched concepts, phrase containment, overlap ratio and a
        small recency bonus. Empty queries and limit values <= 0 return ``[]``
        without touching the cache.

        Args:
            query: Free-text user query.
            chat_id: Optional chat filter; ``None`` searches every chat.
            limit: Maximum number of hits to return.

        Returns:
            Up to ``limit`` :class:`SearchHit` objects sorted by descending
            score, then descending message date.
        """

        tokens = tokenize(query or "")
        if not tokens or limit <= 0:
            return []

        normalized_query = " ".join(tokens)
        key = self._cache_key(chat_id, normalized_query, limit)
        cached = self.cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        messages: list[Message] = list(self.db.list_messages(chat_id))
        hits = self._rank(messages, tokens, normalized_query, limit)
        self.cache.set(key, hits)
        return hits

    def invalidate_cache(self, chat_id: int | None = None) -> int:
        """Invalidate cached search results.

        Args:
            chat_id: When given, only results for that chat are removed.
                When ``None`` every cached search entry is removed.

        Returns:
            The number of cache entries removed.
        """

        if chat_id is None:
            return self.cache.invalidate("search:") + self.cache.invalidate("chat:")
        return self.cache.invalidate(f"search:{chat_id}:") + self.cache.invalidate(
            f"chat:{chat_id}"
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _cache_key(chat_id: int | None, normalized_query: str, limit: int) -> str:
        """Build the cache key for one logical query."""

        scope = "all" if chat_id is None else str(chat_id)
        return f"search:{scope}:{normalized_query}:{limit}"

    def _rank(
        self,
        messages: list[Message],
        tokens: list[str],
        normalized_query: str,
        limit: int,
    ) -> list[SearchHit]:
        """Score ``messages`` against the tokenized query and return top hits."""

        concepts = _query_concepts(tokens)
        expanded_terms: frozenset[str] = frozenset().union(*concepts)
        raw_tokens = frozenset(tokens)
        phrase_bonus_enabled = len(tokens) > 1

        timestamps: list[float] = []
        for message in messages:
            parsed = _parse_timestamp(message.date)
            if parsed is not None:
                timestamps.append(parsed)
        oldest = min(timestamps) if timestamps else 0.0
        newest = max(timestamps) if timestamps else 0.0
        span = newest - oldest

        scored: list[tuple[float, float, int, SearchHit]] = []
        for message in messages:
            message_tokens = set(tokenize(message.text))
            if not message_tokens:
                continue

            concept_score = 0.0
            for concept in concepts:
                if not (concept & message_tokens):
                    continue
                # A direct hit on the query word outranks a synonym-only hit.
                if concept & raw_tokens & message_tokens:
                    concept_score += self.SCORE_PER_CONCEPT
                else:
                    concept_score += self.SCORE_PER_CONCEPT * self.SYNONYM_WEIGHT
            phrase_score = (
                self.MAX_PHRASE_BONUS
                if phrase_bonus_enabled
                and normalized_query in _normalize_text(message.text)
                else 0.0
            )
            overlap = (
                len(expanded_terms & message_tokens) / len(expanded_terms)
                if expanded_terms
                else 0.0
            )
            base_score = concept_score + phrase_score + self.MAX_OVERLAP_BONUS * overlap
            if base_score <= 0.0:
                continue

            parsed = _parse_timestamp(message.date)
            recency = 0.0
            if parsed is not None and span > 0.0:
                recency = self.MAX_RECENCY_BONUS * (parsed - oldest) / span
            score = base_score + recency
            sort_date = parsed if parsed is not None else float("-inf")
            scored.append((score, sort_date, message.message_id, SearchHit(message, score)))

        scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        return [hit for _, _, _, hit in scored[:limit]]
