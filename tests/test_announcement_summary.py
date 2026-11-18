"""Offline tests for announcement summarization (SCRUM-34).

All AI interaction is exercised through fake clients or a monkeypatched
``requests.post``; these tests never touch the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import requests

from src.announcement_summary import (
    EXCERPT_LIMIT,
    MAX_BULLETS,
    MAX_SOURCES,
    AnnouncementSummarizer,
    RayuClient,
)
from src.config import Database, Settings
from src.models import Message

FIXED_DATE = "2026-05-01T09:00:00+00:00"


def make_settings(api_key: str = "") -> Settings:
    """Build deterministic settings with an optional Rayu API key."""

    return Settings(
        rayu_api_key=api_key,
        rayu_base_url="https://rayu.example.invalid/v1",
        rayu_model="test-model",
    )


def make_message(
    message_id: int = 1,
    text: str = "Midterm exam is on 20 May.",
    date: str = FIXED_DATE,
    chat_id: int = 100,
) -> Message:
    """Build a message with sensible fixed defaults."""

    return Message(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        date=date,
    )


class FakeClient:
    """Stand-in for :class:`RayuClient` that records prompts and never dials."""

    def __init__(self, result: Any = "AI summary [1]") -> None:
        self.result = result
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


# ---------------------------------------------------------------------------
# AI path
# ---------------------------------------------------------------------------


def test_fake_client_summary_uses_ai_and_populates_sources() -> None:
    messages = [
        make_message(1, "Midterm exam is on 20 May 2026."),
        make_message(2, "Library closes at 18:00."),
    ]
    client = FakeClient("Exam on 20 May [1]; library closes early [2].")
    summarizer = AnnouncementSummarizer(make_settings(), client=client)

    result = summarizer.summarize(messages, period_label="today")

    assert result["used_ai"] is True
    assert result["summary"] == "Exam on 20 May [1]; library closes early [2]."
    assert result["uncertain"] is False
    assert [source["message_id"] for source in result["sources"]] == [1, 2]
    assert result["sources"][0] == {
        "chat_id": 100,
        "message_id": 1,
        "date": FIXED_DATE,
        "text": "Midterm exam is on 20 May 2026.",
    }
    assert client.calls == 1
    prompt = client.prompts[0]
    assert "[1]" in prompt
    assert "[2]" in prompt
    assert "only" in prompt.lower()
    assert "conflict" in prompt.lower()
    assert "Midterm exam is on 20 May 2026." in prompt


def test_empty_ai_response_falls_back_and_flags_uncertain() -> None:
    messages = [make_message(1, "Exam room changed to B201.")]
    client = FakeClient("")
    summarizer = AnnouncementSummarizer(make_settings(), client=client)

    result = summarizer.summarize(messages)

    assert result["used_ai"] is False
    assert result["summary"] == "- Exam room changed to B201."
    assert result["sources"][0]["message_id"] == 1
    assert result["uncertain"] is True


# ---------------------------------------------------------------------------
# Fallback path
# ---------------------------------------------------------------------------


def test_client_exception_falls_back_to_extractive_summary() -> None:
    messages = [
        make_message(1, "Midterm exam is on 20 May. Bring a pencil."),
        make_message(2, "Library closes at 18:00! Plan ahead."),
        make_message(3, "Club meeting moves to Friday?"),
    ]
    client = FakeClient(RuntimeError("gateway exploded"))
    summarizer = AnnouncementSummarizer(make_settings(), client=client)

    result = summarizer.summarize(messages)

    assert result["used_ai"] is False
    assert result["uncertain"] is True  # AI was attempted and produced nothing
    lines = result["summary"].splitlines()
    assert len(lines) == MAX_BULLETS
    assert lines == [
        "- Midterm exam is on 20 May.",
        "- Library closes at 18:00!",
        "- Club meeting moves to Friday?",
    ]
    assert len(result["sources"]) == 3


def test_constructing_without_api_key_never_touches_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_post(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("network must not be used without an API key")

    monkeypatch.setattr("src.announcement_summary.requests.post", fail_post)
    summarizer = AnnouncementSummarizer(make_settings(api_key=""))

    result = summarizer.summarize([make_message(1, "No class tomorrow.")])

    assert result["used_ai"] is False
    assert result["summary"] == "- No class tomorrow."
    assert result["uncertain"] is False


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_no_messages_reports_no_relevant_information() -> None:
    client = FakeClient("should not be called")
    summarizer = AnnouncementSummarizer(make_settings(), client=client)

    result = summarizer.summarize([], period_label="today")

    assert result == {
        "summary": "No relevant announcements were found for today.",
        "sources": [],
        "uncertain": False,
        "used_ai": False,
    }
    assert client.calls == 0


def test_blank_messages_are_irrelevant_and_do_not_invent_content() -> None:
    client = FakeClient("should not be called")
    summarizer = AnnouncementSummarizer(make_settings(), client=client)

    result = summarizer.summarize(
        [
            make_message(1, ""),
            make_message(2, "   \n\t "),
        ],
        period_label="this week",
    )

    assert result["summary"] == (
        "No relevant announcements were found for this week."
    )
    assert result["sources"] == []
    assert result["uncertain"] is False
    assert result["used_ai"] is False
    assert client.calls == 0


# ---------------------------------------------------------------------------
# Uncertainty heuristic
# ---------------------------------------------------------------------------


def test_conflicting_deadline_dates_are_uncertain() -> None:
    messages = [
        make_message(
            1, "Homework 2 deadline is 2026-05-20.", "2026-05-01T09:00:00+00:00"
        ),
        make_message(
            2, "Homework 2 deadline is 2026-05-27.", "2026-05-01T10:00:00+00:00"
        ),
    ]
    summarizer = AnnouncementSummarizer(make_settings(api_key=""))

    result = summarizer.summarize(messages)

    assert result["uncertain"] is True
    assert result["used_ai"] is False


def test_matching_deadline_dates_are_not_uncertain() -> None:
    messages = [
        make_message(1, "Homework 2 deadline is 2026-05-20."),
        make_message(2, "Homework 2 deadline is 2026-05-20 at 17:00."),
    ]
    summarizer = AnnouncementSummarizer(make_settings(api_key=""))

    result = summarizer.summarize(messages)

    assert result["uncertain"] is False


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def test_sources_are_capped_and_excerpts_truncated() -> None:
    messages = [
        make_message(message_id=index, text=f"Notice {index}: " + "x" * 400)
        for index in range(1, 16)
    ]
    summarizer = AnnouncementSummarizer(make_settings(api_key=""))

    result = summarizer.summarize(messages)

    assert len(messages) > MAX_SOURCES
    assert len(result["sources"]) == MAX_SOURCES
    assert [source["message_id"] for source in result["sources"]] == list(
        range(1, MAX_SOURCES + 1)
    )
    for source in result["sources"]:
        assert len(source["text"]) <= EXCERPT_LIMIT
        assert set(source) == {"chat_id", "message_id", "date", "text"}
    assert EXCERPT_LIMIT <= 200
    assert len(result["summary"].splitlines()) == MAX_BULLETS


# ---------------------------------------------------------------------------
# summarize_today
# ---------------------------------------------------------------------------


def test_summarize_today_filters_by_utc_date_and_chat(db: Database) -> None:
    now = datetime.now(timezone.utc)
    today_iso = now.isoformat()
    yesterday_iso = (now - timedelta(days=1)).isoformat()

    db.add_message(make_message(1, "Math homework worksheet.", today_iso, 100))
    db.add_message(make_message(2, "Physics lab worksheet.", today_iso, 200))
    db.add_message(
        make_message(3, "Old chemistry worksheet.", yesterday_iso, 100)
    )
    db.add_message(make_message(4, "Unparseable worksheet.", "not-a-date", 100))

    summarizer = AnnouncementSummarizer(make_settings(api_key=""), db=db)

    result = summarizer.summarize_today(chat_id=100)
    assert [source["message_id"] for source in result["sources"]] == [1]
    assert result["used_ai"] is False
    assert result["uncertain"] is False
    assert "Math homework worksheet." in result["summary"]

    all_today = summarizer.summarize_today()
    assert [source["message_id"] for source in all_today["sources"]] == [1, 2]


def test_summarize_today_without_database_is_empty() -> None:
    summarizer = AnnouncementSummarizer(make_settings(api_key=""))

    result = summarizer.summarize_today(chat_id=100)

    assert result["summary"] == "No relevant announcements were found for today."
    assert result["sources"] == []
    assert result["used_ai"] is False


# ---------------------------------------------------------------------------
# RayuClient contract (monkeypatched, no network)
# ---------------------------------------------------------------------------


def test_rayu_client_request_shape_and_null_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": None}}]}

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr("src.announcement_summary.requests.post", fake_post)
    client = RayuClient(
        api_key="secret-key",
        base_url="https://rayu.example.invalid/v1/",
        model="test-model",
    )

    assert client.complete("Summarize these messages") == ""

    assert captured["url"] == (
        "https://rayu.example.invalid/v1/chat/completions"
    )
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["timeout"] == 30
    payload = captured["json"]
    assert payload["model"] == "test-model"
    assert payload["messages"] == [
        {"role": "user", "content": "Summarize these messages"}
    ]
    assert payload["max_tokens"] >= 64
    assert payload["max_tokens"] == 512
    assert payload["stream"] is False


def test_rayu_client_returns_content_and_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OkResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "  hello [1]  "}}]}

    monkeypatch.setattr(
        "src.announcement_summary.requests.post",
        lambda *args, **kwargs: OkResponse(),
    )
    client = RayuClient("key", "https://rayu.example.invalid/v1", "test-model")
    assert client.complete("prompt") == "  hello [1]  "

    class ErrorResponse:
        def raise_for_status(self) -> None:
            raise requests.HTTPError("500 Server Error")

    monkeypatch.setattr(
        "src.announcement_summary.requests.post",
        lambda *args, **kwargs: ErrorResponse(),
    )
    with pytest.raises(requests.HTTPError):
        client.complete("prompt")
