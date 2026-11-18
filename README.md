# JumnuyBot

JumnuyBot is a Telegram student assistant. It connects to course groups (with
explicit admin consent), ingests messages, makes announcements searchable,
summarizes them with the Rayu AI gateway, and keeps track of assignment
deadlines and reminders.

Sprint 3 (SCRUM-31--38) delivers the shared foundation and the feature modules
around it:

| Area | Purpose |
| --- | --- |
| Foundation (SCRUM-38) | `Settings`, five-table SQLite storage, shared models, offline wiring |
| Group connection | Bot-added consent flow and admin-only authorization |
| Message ingestion | Duplicate-safe message capture for authorized groups |
| Search | Keyword search with a TTL cache |
| Summary | Announcement summaries with cited sources and AI fallback |
| Access control | Admin and authorized-group checks |
| Source references | Telegram message links and citation rendering |
| Reliability | Consistent behavior across the modules above |

## Architecture

```text
Telegram  ->  server (bot modules)  ->  SQLite five-table store + TTL cache  ->  Rayu AI gateway
```

The storage layer is plain `sqlite3` from the standard library: users, groups,
messages, assignments and reminders. No external database is required.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values locally
```

`main.py` is offline-safe: it initializes the database and prints a status
report without contacting Telegram or Rayu.

```bash
python main.py
```

## Running the bot

Install the pinned dependencies, then start the bot with long polling:

```bash
pip install -r requirements.txt
python -m src.main_bot
```

`python -m src.main_bot` requires the `TELEGRAM_BOT_TOKEN` environment
variable. Provide it by exporting it in your shell or by placing it in `.env`
(see `.env.example` for the full list of variables):

```bash
export TELEGRAM_BOT_TOKEN=...   # or set it in .env
```

The token is only needed at run time. Importing the modules and running the
tests needs no token, and `python main.py` remains an offline demo: it creates
the five SQLite tables and makes no Telegram or AI calls.

### Commands

| Command | Purpose |
| --- | --- |
| `/start` | Greet the chat and show a short usage hint |
| `/ask <question>` | Search ingested messages and reply with cited sources |
| `/today` | Summarize today's announcements with cited sources |
| `/authorize` | Admin-only: approve the group so the bot may read it |
| `/revoke` | Admin-only: revoke the group's authorization |

The bot must be added to a group and approved by a group admin before it
reads or answers anything. In groups, plain questions are only answered when
the bot is `@mention`ed or addressed with `/ask`.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token (secret) |
| `RAYU_API_KEY` | empty | Rayu gateway API key (secret) |
| `RAYU_BASE_URL` | `https://gateway.rayucode.com/v1` | Rayu API endpoint |
| `RAYU_MODEL` | `deepseek-v4.1-flash-1017-cloud` | Rayu model name |
| `DATABASE_PATH` | `jumnuybot.db` | SQLite file path (`:memory:` allowed) |
| `CACHE_TTL_SECONDS` | `300` | Search cache lifetime in seconds |
| `DEFAULT_TIMEZONE` | `UTC` | Default timezone for users |
| `REMINDER_LEAD_MINUTES` | `60` | Default reminder lead time |
| `ADMIN_USER_IDS` | empty | Comma-separated Telegram ids allowed to authorize groups |
| `BOT_USERNAME` | `jumnuybot` | Bot username used in message links |

## Tests

Tests are offline and use in-memory SQLite databases only.

```bash
python -m pytest -q
```

## Notes

- `.env` and the `docx/` directory are private and must never be committed;
  both are covered by `.gitignore`.
- All timestamps are timezone-aware ISO-8601 strings.
