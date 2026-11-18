"""Tests for SCRUM-36 Telegram deep links, parsing and source rendering."""

from __future__ import annotations

import pytest

from src.models import SourceRef
from src.source_references import (
    format_source,
    message_link,
    parse_link,
    render_sources,
)

SUPERGROUP = -1001234567890
CHANNEL = 1234567890


class TestMessageLink:
    """Link construction for private, supergroup and public chats."""

    def test_private_supergroup_link(self) -> None:
        assert message_link(SUPERGROUP, 42) == "https://t.me/c/1234567890/42"

    @pytest.mark.parametrize(
        ("chat_id", "channel"),
        [
            (-1001234567890, 1234567890),
            (-1002222222222, 2222222222),
            (-1003000000000, 3000000000),
            (-1001000000001, 1000000001),
        ],
    )
    def test_supergroup_prefix_is_stripped(self, chat_id: int, channel: int) -> None:
        assert message_link(chat_id, 7) == f"https://t.me/c/{channel}/7"

    def test_accepts_string_ids_and_whitespace(self) -> None:
        assert (
            message_link(" -1001234567890 ", " 42 ") == "https://t.me/c/1234567890/42"
        )

    @pytest.mark.parametrize("username", ["jumnuybot", "@jumnuybot", "  @jumnuybot  "])
    def test_public_username_with_and_without_at(self, username: str) -> None:
        assert message_link(SUPERGROUP, 42, username) == "https://t.me/jumnuybot/42"

    def test_public_username_wins_over_chat_id(self) -> None:
        assert message_link(-1009999999999, 3, "@news") == "https://t.me/news/3"

    def test_bare_at_username_falls_back_to_private_link(self) -> None:
        assert message_link(SUPERGROUP, 42, "@") == "https://t.me/c/1234567890/42"

    def test_other_negative_chat_uses_absolute_value(self) -> None:
        assert message_link(-123456, 9) == "https://t.me/c/123456/9"

    def test_positive_chat_without_username_is_best_effort(self) -> None:
        assert message_link(123456, 9) == "https://t.me/c/123456/9"

    @pytest.mark.parametrize(
        "chat_id",
        [None, "", "   ", "not-a-number", "1.5", [], {}, 3.5],
    )
    def test_invalid_chat_id_returns_empty(self, chat_id: object) -> None:
        assert message_link(chat_id, 42) == ""  # type: ignore[arg-type]

    @pytest.mark.parametrize("message_id", [None, "", "abc", 0, -1, 2.5, []])
    def test_invalid_message_id_returns_empty(self, message_id: object) -> None:
        assert message_link(SUPERGROUP, message_id) == ""  # type: ignore[arg-type]

    def test_zero_chat_id_returns_empty(self) -> None:
        assert message_link(0, 42) == ""


class TestParseLink:
    """Parsing of the link shapes Telegram produces."""

    def test_parse_private_link(self) -> None:
        assert parse_link("https://t.me/c/1234567890/42") == (SUPERGROUP, 42)

    @pytest.mark.parametrize("message_id", [1, 42, 999999])
    def test_private_link_round_trip(self, message_id: int) -> None:
        link = message_link(SUPERGROUP, message_id)
        assert parse_link(link) == (SUPERGROUP, message_id)

    def test_parse_private_full_negative_channel(self) -> None:
        assert parse_link("https://t.me/c/-1001234567890/42") == (SUPERGROUP, 42)

    def test_parse_public_link_uses_zero_sentinel(self) -> None:
        assert parse_link("https://t.me/jumnuybot/42") == (0, 42)

    def test_parse_public_preview_link(self) -> None:
        assert parse_link("https://t.me/s/jumnuybot/42") == (0, 42)

    def test_parse_tolerates_http_www_and_trailing_slash(self) -> None:
        assert parse_link("http://t.me/c/1234567890/42/") == (SUPERGROUP, 42)
        assert parse_link("https://www.t.me/jumnuybot/42") == (0, 42)
        assert parse_link("https://t.me/jumnuybot/42?single") == (0, 42)
        assert parse_link("  https://t.me/c/1234567890/42  ") == (SUPERGROUP, 42)

    def test_parse_tg_privatepost(self) -> None:
        assert parse_link("tg://privatepost?channel=1234567890&post=42") == (
            SUPERGROUP,
            42,
        )

    def test_parse_tg_privatepost_with_extra_params(self) -> None:
        link = "tg://privatepost?channel=1234567890&post=42&single=1"
        assert parse_link(link) == (SUPERGROUP, 42)

    @pytest.mark.parametrize(
        "link",
        [
            None,
            "",
            "   ",
            "not-a-link",
            "https://t.me/c/1234567890",
            "https://t.me/c/1234567890/abc",
            "https://t.me/c/1234567890/0",
            "https://t.me/c/abc/42",
            "https://t.me/jumnuybot",
            "https://t.me/jumnuybot/abc",
            "https://t.me/jumnuybot/0",
            "https://example.com/c/1234567890/42",
            "ftp://t.me/c/1234567890/42",
            "tg://privatepost?channel=1234567890",
            "tg://privatepost?channel=abc&post=42",
            "tg://privatepost?channel=1234567890&post=abc",
            "tg://resolve?domain=jumnuybot",
            12345,
        ],
    )
    def test_malformed_links_return_none(self, link: object) -> None:
        assert parse_link(link) is None  # type: ignore[arg-type]


class TestFormatSource:
    """Single-citation formatting."""

    def test_without_index_is_plain_url(self) -> None:
        assert format_source(SUPERGROUP, 42) == "https://t.me/c/1234567890/42"

    def test_with_index_is_numbered(self) -> None:
        assert format_source(SUPERGROUP, 42, index=1) == (
            "[1] https://t.me/c/1234567890/42"
        )

    def test_with_username_and_index(self) -> None:
        assert format_source(SUPERGROUP, 42, "@jumnuybot", 3) == (
            "[3] https://t.me/jumnuybot/42"
        )

    @pytest.mark.parametrize(("chat_id", "message_id"), [(0, 1), (SUPERGROUP, 0)])
    def test_invalid_ids_return_empty(self, chat_id: int, message_id: int) -> None:
        assert format_source(chat_id, message_id) == ""
        assert format_source(chat_id, message_id, index=1) == ""


class TestRenderSources:
    """Numbered rendering of mixed source collections."""

    def test_renders_numbered_dicts_and_source_refs(self) -> None:
        sources = [
            SourceRef(chat_id=SUPERGROUP, message_id=42),
            {"chat_id": SUPERGROUP, "message_id": 43},
            {"chat_id": SUPERGROUP, "message_id": 44, "username": "@jumnuybot"},
        ]
        assert render_sources(sources) == (
            "[1] https://t.me/c/1234567890/42\n"
            "[2] https://t.me/c/1234567890/43\n"
            "[3] https://t.me/jumnuybot/44"
        )

    def test_skips_unlinkable_entries_without_consuming_numbers(self) -> None:
        sources = [
            {"chat_id": SUPERGROUP, "message_id": 42},
            None,
            {},
            {"chat_id": None, "message_id": None},
            {"chat_id": SUPERGROUP, "message_id": 0},
            "junk",
            {"chat_id": SUPERGROUP, "message_id": 43},
        ]
        assert render_sources(sources) == (
            "[1] https://t.me/c/1234567890/42\n"
            "[2] https://t.me/c/1234567890/43"
        )

    def test_explicit_url_wins(self) -> None:
        sources = [
            SourceRef(
                chat_id=SUPERGROUP,
                message_id=42,
                url="https://example.invalid/cached",
            ),
            {"chat_id": SUPERGROUP, "message_id": 43, "url": "https://example.invalid/2"},
        ]
        assert render_sources(sources) == (
            "[1] https://example.invalid/cached\n"
            "[2] https://example.invalid/2"
        )

    def test_source_ref_url_derived_from_ids(self) -> None:
        assert render_sources([SourceRef(chat_id=SUPERGROUP, message_id=7)]) == (
            "[1] https://t.me/c/1234567890/7"
        )

    @pytest.mark.parametrize("sources", [None, [], (), "not-a-list"])
    def test_empty_or_malformed_container_returns_empty(self, sources: object) -> None:
        assert render_sources(sources) == ""  # type: ignore[arg-type]

    def test_non_iterable_returns_empty(self) -> None:
        assert render_sources(42) == ""  # type: ignore[arg-type]
