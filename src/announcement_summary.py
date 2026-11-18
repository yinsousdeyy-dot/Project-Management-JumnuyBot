"""AI and extractive announcement summarization (SCRUM-34).

The summarizer turns a batch of stored Telegram :class:`~src.models.Message`
objects into a short announcement digest. It prefers the hosted Rayu chat
completion endpoint when an API key is configured, but is fully offline-safe:
without a key (or whenever the network call fails) it falls back to a
deterministic extractive summary built from the first sentence of each message.

Every summary carries citation sources so the caller can point users back at
the original messages. No network request is ever made in tests or merely by
constructing :class:`AnnouncementSummarizer` with an empty API key.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Sequence

import requests

from src.config import Database, Settings
from src.models import Message

#: Maximum number of messages included in a summary and its source list.
MAX_SOURCES = 10

#: Maximum number of bullet points produced by the extractive fallback.
MAX_BULLETS = 3

#: Maximum length of a source excerpt, in characters.
EXCERPT_LIMIT = 200

#: Generation budget for the hosted model. Reasoning models return ``null``
#: content when truncated, so this must stay comfortably above 64 tokens.
MAX_TOKENS = 512

_DEFAULT_PERIOD = "today"

# Date-like tokens used by the uncertainty heuristic. These are intentionally
# shallow: the goal is a cheap "messages mention different deadlines" signal,
# not full natural-language date parsing.
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    re.compile(r"\b\d{1,2}\s*\.\s*\d{1,2}(?:\s*\.\s*\d{2,4})?\b"),
    re.compile(
        r"\b\d{1,2}\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
        r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\s+\d{1,2}(?:,?\s+\d{4})?\b",
        re.IGNORECASE,
    ),
)

_MONTH_WORDS = frozenset(
    {
        "jan",
        "january",
        "feb",
        "february",
        "mar",
        "march",
        "apr",
        "april",
        "may",
        "jun",
        "june",
        "jul",
        "july",
        "aug",
        "august",
        "sep",
        "september",
        "oct",
        "october",
        "nov",
        "november",
        "dec",
        "december",
    }
)

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "these",
        "those",
        "from",
        "will",
        "would",
        "should",
        "could",
        "are",
        "was",
        "were",
        "have",
        "has",
        "had",
        "not",
        "you",
        "your",
        "our",
        "their",
        "they",
        "them",
        "there",
        "here",
        "today",
        "tomorrow",
        "tonight",
        "yesterday",
        "please",
        "student",
        "students",
        "announcement",
        "announcements",
        "update",
        "updates",
        "about",
        "into",
        "over",
        "under",
        "also",
        "but",
        "can",
        "may",
        "might",
        "must",
        "shall",
        "does",
        "did",
        "done",
        "been",
        "being",
        "its",
        "his",
        "her",
        "him",
        "she",
        "who",
        "whom",
        "which",
        "what",
        "when",
        "where",
        "why",
        "how",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "now",
        "new",
    }
)

_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


# ---------------------------------------------------------------------------
# Rayu API client
# ---------------------------------------------------------------------------


class RayuClient:
    """Minimal client for the Rayu chat-completions gateway (SCRUM-34).

    The client is inert until :meth:`complete` is called. It sends a single
    user message and returns the assistant content, treating a missing or
    ``null`` content field as an empty string.

    Args:
        api_key: Bearer token for the gateway.
        base_url: Gateway base URL, for example
            ``https://gateway.rayucode.com/v1``.
        model: Model identifier sent in the request payload.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        """Return the fully qualified chat-completions URL."""

        return f"{self.base_url.rstrip('/')}/chat/completions"

    def complete(self, prompt: str) -> str:
        """Send ``prompt`` to the gateway and return the assistant text.

        Raises:
            requests.HTTPError: When the gateway responds with a non-2xx
                status. Callers that must not fail should catch exceptions.
        """

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": MAX_TOKENS,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(
            self.endpoint,
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            return ""
        return content if isinstance(content, str) else ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_text(message: Message) -> str:
    """Return the trimmed text of ``message`` (never ``None``)."""

    return (message.text or "").strip()


def _excerpt(text: str, limit: int = EXCERPT_LIMIT) -> str:
    """Return ``text`` shortened to at most ``limit`` characters."""

    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _first_sentence(text: str, limit: int = EXCERPT_LIMIT) -> str:
    """Return the first sentence of ``text`` capped at ``limit`` characters."""

    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    sentence = _SENTENCE_SPLIT_RE.split(cleaned, maxsplit=1)[0].strip()
    return _excerpt(sentence or cleaned, limit)


def _extractive_summary(messages: Sequence[Message]) -> str:
    """Build a deterministic bullet summary from up to three messages.

    The first sentence of each of the first :data:`MAX_BULLETS` messages is
    rendered as a ``"- "`` bullet. This never consults the network and never
    invents content.
    """

    bullets: list[str] = []
    for message in messages[:MAX_BULLETS]:
        sentence = _first_sentence(_as_text(message))
        if sentence:
            bullets.append(f"- {sentence}")
    return "\n".join(bullets)


def _normalise_date_token(token: str) -> str:
    """Normalise a date-like token for cheap equality comparisons."""

    return re.sub(r"\s+", " ", token.strip().lower()).strip(" ,.")


def _extract_date_tokens(text: str) -> set[str]:
    """Return the normalised date-like tokens found in ``text``."""

    tokens: set[str] = set()
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text or ""):
            tokens.add(_normalise_date_token(match.group(0)))
    return tokens


def _extract_keywords(text: str) -> set[str]:
    """Return lowercase topical keywords (length >= 3, no stopwords/months)."""

    words = _WORD_RE.findall((text or "").lower())
    return {
        word
        for word in words
        if word not in _STOPWORDS and word not in _MONTH_WORDS
    }


def _has_conflicting_dates(messages: Sequence[Message]) -> bool:
    """Return whether two related messages cite different dates.

    The heuristic flags a pair when both messages contain at least one
    date-like token, their date-token sets are disjoint, and they share at
    least two keywords (so unrelated announcements are not compared).
    """

    candidates: list[tuple[set[str], set[str]]] = []
    for message in messages:
        text = _as_text(message)
        dates = _extract_date_tokens(text)
        if not dates:
            continue
        candidates.append((_extract_keywords(text), dates))

    for index, (keywords_a, dates_a) in enumerate(candidates):
        for keywords_b, dates_b in candidates[index + 1 :]:
            if len(keywords_a & keywords_b) >= 2 and not (dates_a & dates_b):
                return True
    return False


def _parse_message_date(value: str) -> date | None:
    """Parse an ISO-8601 message timestamp into its UTC calendar date."""

    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).date()
    return parsed.date()


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------


class AnnouncementSummarizer:
    """Summarize stored announcements with AI and extractive fallback.

    Args:
        settings: Runtime configuration; ``rayu_api_key`` enables the AI path.
        db: Optional database used by :meth:`summarize_today`.
        client: Optional pre-built client exposing ``complete(prompt) -> str``.
            Tests inject fakes here so no network access occurs.

    SCRUM-34: the AI path is best-effort. Any missing key, empty answer, or
    exception degrades to a deterministic extractive summary instead of
    propagating an error to the caller.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        if client is not None:
            self.client: Any | None = client
        elif getattr(settings, "rayu_api_key", ""):
            self.client = RayuClient(
                api_key=settings.rayu_api_key,
                base_url=settings.rayu_base_url,
                model=settings.rayu_model,
            )
        else:
            self.client = None

    # -- public API --------------------------------------------------------

    def summarize(
        self,
        messages: list[Message],
        period_label: str = _DEFAULT_PERIOD,
    ) -> dict[str, Any]:
        """Summarize ``messages`` into a digest with citation sources.

        Args:
            messages: Candidate announcements. Blank messages are ignored.
            period_label: Human-readable period used in the empty result,
                for example ``"today"`` or ``"this week"``.

        Returns:
            A dictionary with ``summary`` (str), ``sources`` (list of dicts
            with ``chat_id``, ``message_id``, ``date`` and ``text``),
            ``uncertain`` (bool) and ``used_ai`` (bool). When no usable
            message exists the result explicitly states that nothing relevant
            was found; no content is ever invented.
        """

        period = (period_label or "").strip() or _DEFAULT_PERIOD
        candidates = list(messages or [])
        relevant = [message for message in candidates if _as_text(message)]
        used = relevant[:MAX_SOURCES]

        if not used:
            return {
                "summary": (
                    f"No relevant announcements were found for {period}."
                ),
                "sources": [],
                "uncertain": False,
                "used_ai": False,
            }

        sources = [
            {
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "date": message.date,
                "text": _excerpt(message.text),
            }
            for message in used
        ]

        summary_text = ""
        used_ai = False
        if self.client is not None:
            try:
                answer = self.client.complete(self._build_prompt(used, period))
            except Exception:
                answer = ""
            if isinstance(answer, str):
                summary_text = answer.strip()
                used_ai = bool(summary_text)

        if not used_ai:
            summary_text = _extractive_summary(used) or (
                f"No relevant announcements were found for {period}."
            )

        conflicting_dates = _has_conflicting_dates(used)
        # An attempted AI answer that came back empty is itself a reason to
        # tell the user the digest is uncertain.
        uncertain = conflicting_dates or (self.client is not None and not used_ai)

        return {
            "summary": summary_text,
            "sources": sources,
            "uncertain": uncertain,
            "used_ai": used_ai,
        }

    def summarize_today(self, chat_id: int | None = None) -> dict[str, Any]:
        """Summarize messages whose timestamps fall on today's UTC date.

        Args:
            chat_id: Optional Telegram chat filter.

        Returns:
            The same structure as :meth:`summarize`, with
            ``period_label="today"``. Messages with unparseable dates are
            excluded. Without a database the empty result is returned.
        """

        if self.db is None:
            return self.summarize([], period_label=_DEFAULT_PERIOD)

        today = datetime.now(timezone.utc).date()
        todays_messages = [
            message
            for message in self.db.list_messages(chat_id)
            if _parse_message_date(message.date) == today
        ]
        return self.summarize(todays_messages, period_label=_DEFAULT_PERIOD)

    # -- internals ---------------------------------------------------------

    def _build_prompt(self, messages: Sequence[Message], period: str) -> str:
        """Build the grounded summarization prompt sent to the AI client."""

        lines = [
            "You are a student announcement summarizer.",
            f"Summarize only the Telegram messages below for the period: {period}.",
            "Rules:",
            "- Use only the provided messages and never invent details.",
            "- Cite supporting messages with their [n] index.",
            "- If messages conflict (for example, different deadlines for the "
            "same topic), state the conflict explicitly.",
            "Messages:",
        ]
        for index, message in enumerate(messages, start=1):
            lines.append(f"[{index}] ({message.date}) {_as_text(message)}")
        lines.append("Write a concise bullet-point summary of the announcements.")
        return "\n".join(lines)
