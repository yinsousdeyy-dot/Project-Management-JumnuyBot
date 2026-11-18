"""Process entrypoint for the live JumnuyBot (SCRUM-46).

Run it from the repository root with ``python -m src.main_bot``. The token is
read from ``TELEGRAM_BOT_TOKEN`` (or ``.env``) when the bot starts, never at
import time, and is never printed.
"""

from __future__ import annotations

from src.telegram_bot import run

__all__ = ["main"]


def main() -> int:
    """Start long polling and return the process exit code.

    Returns:
        ``0`` after a clean shutdown.
    """

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
