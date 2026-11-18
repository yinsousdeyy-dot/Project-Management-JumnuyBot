<<<<<<< HEAD
"""Bot entrypoint and application wiring."""


def main() -> None:
    """Start the project management bot."""
    print("Project Management JumnuyBot")


if __name__ == "__main__":
    main()
=======
"""JumnuyBot entry point and offline-safe application wiring (SCRUM-38).

This module never contacts Telegram or the Rayu gateway. It only assembles
settings and the initialized SQLite database, then attaches feature modules
that happen to be importable. That keeps ``main.py`` useful in CI and on a
machine without credentials.
"""

from __future__ import annotations

import importlib
from typing import Any

from src.config import Database, Settings, load_settings

# (app key, module path, class name, constructor args in contract order)
_OPTIONAL_MODULES: tuple[tuple[str, str, str, tuple[Any, ...]], ...] = (
    ("access_controller", "src.access_control", "AccessController", (None, None)),
    ("group_connector", "src.group_connector", "GroupConnector", (None, None)),
    ("message_ingestor", "src.message_ingestion", "MessageIngestor", (None,)),
    ("message_search", "src.message_search", "MessageSearch", (None,)),
    ("summarizer", "src.announcement_summary", "AnnouncementSummarizer", (None, None)),
)


def _build_optional_modules(
    settings: Settings, db: Database
) -> dict[str, Any]:
    """Instantiate feature modules that exist, silently skipping the rest.

    Each constructor takes either ``(settings, db)``, ``(db, settings)`` or
    ``(db,)``. The first argument placeholder is replaced per module below.
    """

    built: dict[str, Any] = {}
    argument_map: dict[str, tuple[Any, ...]] = {
        "access_controller": (settings, db),
        "group_connector": (db, settings),
        "message_ingestor": (db,),
        "message_search": (db,),
        "summarizer": (settings, db),
    }
    for key, module_name, class_name, _ in _OPTIONAL_MODULES:
        try:
            module = importlib.import_module(module_name)
            component = getattr(module, class_name)
            built[key] = component(*argument_map[key])
        except Exception:
            # The module may not exist yet, may require optional dependencies,
            # or may fail to import while other workers are still landing code.
            continue
    return built


def build_app(settings: Settings | None = None) -> dict[str, Any]:
    """Build the offline application container.

    Args:
        settings: Optional pre-built settings; loaded from the environment
            when omitted.

    Returns:
        A dictionary with ``settings``, an initialized ``db``, and any feature
        modules that could be constructed without network access.
    """

    resolved = settings or load_settings()
    db = Database(resolved.database_path)
    db.initialize()
    app: dict[str, Any] = {"settings": resolved, "db": db}
    app.update(_build_optional_modules(resolved, db))
    return app


def main() -> int:
    """Print a short offline status report and return an exit code."""

    settings = load_settings()
    app = build_app(settings)
    db: Database = app["db"]

    tables = db.query(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    names = [row["name"] for row in tables]
    extras = sorted(key for key in app if key not in {"settings", "db"})

    print("JumnuyBot - Sprint 3 foundation (SCRUM-38)")
    print(f"  bot username: @{settings.bot_username}")
    print(f"  database: {settings.database_path}")
    print(f"  tables ({len(names)}): {', '.join(names)}")
    print(f"  optional modules: {', '.join(extras) if extras else 'none'}")
    print("  mode: offline (no Telegram or Rayu network calls made)")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
>>>>>>> 2d01164c5c65fbe09080407175e488694b742d20
