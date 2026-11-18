"""Application factory and long-polling runner for the live bot (SCRUM-46).

This is the final wiring layer. It glues three already-complete pieces
together:

* :mod:`src.config` -- runtime :class:`~src.config.Settings`;
* :mod:`src.bot_services` -- the :class:`~src.bot_services.BotServices`
  composition root over every Sprint 3 module;
* :mod:`src.bot_handlers` -- the handler callables and their registration.

The module is deliberately import-safe: importing it never reads the
environment, never constructs a database and never requires a token. Token
resolution happens inside :func:`build_application` (and therefore inside
:func:`run`), so tests and tooling can import the application entrypoints
without any credentials.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application

from src.bot_handlers import register_handlers
from src.bot_services import BotServices, build_services
from src.config import Settings, load_settings

__all__ = ["build_application", "run"]

logger = logging.getLogger(__name__)

#: Fixed, non-secret message raised when no usable token is configured.
_MISSING_TOKEN_MESSAGE = (
    "TELEGRAM_BOT_TOKEN is not set. Export it or add it to .env "
    "(see .env.example)."
)


def build_application(
    settings: Settings | None = None,
    *,
    services: BotServices | None = None,
    token: str | None = None,
) -> Application:
    """Build the :class:`~telegram.ext.Application` with handlers registered.

    Args:
        settings: Optional pre-built settings. When omitted, the settings are
            loaded from the environment -- unless ``services`` is supplied, in
            which case its existing settings are reused so that injecting a
            service bundle never triggers an environment read.
        services: Optional pre-built :class:`BotServices`. A fresh bundle is
            built from ``settings`` when omitted. Building services never
            contacts Telegram or the Rayu gateway.
        token: Optional explicit bot token. Falls back to
            ``settings.telegram_bot_token`` when omitted.

    Returns:
        An ``Application`` with every handler from
        :func:`src.bot_handlers.register_handlers` attached and the service
        bundle stored under ``app.bot_data["services"]``.

    Raises:
        RuntimeError: If no non-blank token can be resolved. The message names
            ``TELEGRAM_BOT_TOKEN`` and never contains any secret value.
    """

    resolved_settings = settings
    if resolved_settings is None:
        if services is not None:
            resolved_settings = services.settings
        else:
            resolved_settings = load_settings()

    resolved_services = (
        services if services is not None else build_services(resolved_settings)
    )

    # Only validate the token here, at call time; importing this module must
    # remain token-free.
    resolved_token = token if token else resolved_settings.telegram_bot_token
    if not resolved_token or not str(resolved_token).strip():
        raise RuntimeError(_MISSING_TOKEN_MESSAGE)
    resolved_token = str(resolved_token).strip()

    app = (
        Application.builder()
        .token(resolved_token)
        .concurrent_updates(False)
        .build()
    )
    app.bot_data["services"] = resolved_services
    register_handlers(app)
    return app


def run(settings: Settings | None = None) -> None:
    """Start long polling for every supported update type.

    Blocks until the application is shut down (for example via
    ``KeyboardInterrupt``). The token is resolved by
    :func:`build_application`, so a missing ``TELEGRAM_BOT_TOKEN`` raises
    :class:`RuntimeError` before any network activity.

    Args:
        settings: Optional pre-built settings; loaded from the environment
            when omitted.
    """

    app = build_application(settings)
    logger.info("JumnuyBot polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
