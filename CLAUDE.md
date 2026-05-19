# Navictus News Watch

## What this is
A scheduled agent that monitors defense, naval, and maritime news sources for items relevant to Navictus (a Portuguese USV startup), filters them with Claude, and delivers a digest via Telegram 3x/week.

## Architecture
- Single Python project, runs as a scheduled GitHub Action (Mon/Wed/Fri 7am)
- Reads sources from `sources.yaml` (curated, version-controlled)
- Fetches new items (RSS + custom scrapers + manual_check reminders)
- Deduplicates against SQLite `seen.db`
- Runs each new item through a Claude relevance filter
- For "sourced_post" candidates, runs an enrichment step that looks for citable PDFs
- Sends formatted digest to a Telegram bot

## Conventions
- Python 3.12, venv in `.venv/`
- Secrets in `.env` (gitignored); never logged, never committed
- Three module categories in `lib/`: fetch, filter, enrich, store, deliver
- Prompts live in `prompts/*.md` as plain markdown, not Python strings
- All Claude API calls use the official `anthropic` Python SDK
- Use `claude-sonnet-4-5` model (or latest available — verify in docs) for the relevance filter
- Log decisions to stdout (GitHub Actions captures these) — relevance scores, what got filtered out, what got through
- Defensive about external sources: any RSS/scraper failure should log a warning, NOT crash the whole run
- One source failing must never block the others

## Build order (do not deviate without asking me first)
1. lib/store.py — SQLite seen-URL store
2. lib/fetch.py — RSS + scraper dispatch
3. lib/filter.py + prompts/relevance.md — Claude relevance call
4. lib/enrich.py + prompts/enrichment.md — sourced-post check
5. lib/deliver.py — Telegram formatting and send
6. run.py — orchestrator
7. .github/workflows/run.yml — scheduled run

## Project structure to create now (empty files, no code yet)
navictus-watch/
├── .github/workflows/run.yml
├── CLAUDE.md
├── lib/
│   ├── __init__.py
│   ├── fetch.py
│   ├── filter.py
│   ├── enrich.py
│   ├── store.py
│   └── deliver.py
├── prompts/
│   ├── relevance.md
│   └── enrichment.md
├── requirements.txt
├── run.py
└── README.md (already exists, leave it)
