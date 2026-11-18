"""Offline tests for the bot entrypoint (SCRUM-46).

Every test here is network-free and polling-free: no Telegram request is
made, no updater is started and no real token is needed. The only token-shaped
value used is an obvious placeholder.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from telegram import Update
from telegram.ext import Application, CommandHandler

from src import main_bot, telegram_bot
from src.bot_handlers import on_error
from src.bot_services import BotServices, build_services
from src.config import Settings
from src.telegram_bot import build_application

_REPO_ROOT = Path(__file__).resolve().parents[1]

_TEST_TOKEN = "123456:TEST-token"


def _offline_settings(**overrides: object) -> Settings:
    """Return test settings backed by an in-memory database."""

    values: dict[str, object] = {"database_path": ":memory:"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_modules_import_without_a_token() -> None:
    """Importing the entrypoints in a token-less process must succeed."""

    env = dict(os.environ)
    env["TELEGRAM_BOT_TOKEN"] = ""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import src.telegram_bot, src.main_bot; print('offline import ok')",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "offline import ok" in result.stdout


def test_missing_token_raises_runtime_error() -> None:
    """A blank token only fails at build time, with a TELEGRAM_BOT_TOKEN name."""

    with pytest.raises(RuntimeError) as excinfo:
        build_application(_offline_settings())

    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


def test_whitespace_token_raises_runtime_error() -> None:
    """A whitespace-only token is treated as missing."""

    with pytest.raises(RuntimeError) as excinfo:
        build_application(_offline_settings(telegram_bot_token="   "))

    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


def test_build_application_returns_application_with_services() -> None:
    """A resolvable token yields a wired Application holding BotServices."""

    app = build_application(_offline_settings(telegram_bot_token=_TEST_TOKEN))

    assert isinstance(app, Application)
    assert isinstance(app.bot_data["services"], BotServices)


def test_handlers_are_registered() -> None:
    """The app exposes ask/today commands plus exactly one error handler."""

    app = build_application(_offline_settings(telegram_bot_token=_TEST_TOKEN))

    group = app.handlers[0]
    commands = {
        command
        for handler in group
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert {"ask", "today"} <= commands

    error_callbacks = list(app.error_handlers)
    assert error_callbacks.count(on_error) == 1


def test_injected_services_and_token_skip_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit services and token are used without loading settings or env."""

    services = build_services(_offline_settings())

    def _unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("build_application read the environment")

    monkeypatch.setattr(telegram_bot, "load_settings", _unexpected)
    monkeypatch.setattr(telegram_bot, "build_services", _unexpected)

    app = build_application(services=services, token=_TEST_TOKEN)

    assert app.bot_data["services"] is services


def test_run_polls_with_all_update_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() delegates to run_polling with every supported update type."""

    recorded: dict[str, object] = {}

    class _FakeApplication:
        def run_polling(self, **kwargs: object) -> None:
            recorded.update(kwargs)

    monkeypatch.setattr(
        telegram_bot, "build_application", lambda settings=None: _FakeApplication()
    )

    telegram_bot.run()

    assert recorded == {"allowed_updates": Update.ALL_TYPES}


def test_main_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() starts the runner and reports a clean exit code."""

    calls: list[bool] = []
    monkeypatch.setattr(main_bot, "run", lambda: calls.append(True))

    assert main_bot.main() == 0
    assert calls == [True]
