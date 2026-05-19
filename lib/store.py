"""
SQLite-backed store of URLs the news-watch agent has already processed.

Tracks every item we've encountered so we don't re-process or re-deliver it,
along with the verdict the relevance filter produced and its relevance score.
"""

import argparse
import os
import sqlite3
from typing import Any

DEFAULT_DB_PATH = "seen.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    url         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    title       TEXT,
    first_seen  DATETIME DEFAULT CURRENT_TIMESTAMP,
    verdict     TEXT,
    relevance   INTEGER,
    last_action DATETIME
)
"""


def _resolve_path(path: str | None = None) -> str:
    if path is not None:
        return path
    return os.environ.get("SEEN_DB_PATH", DEFAULT_DB_PATH)


def init_db(path: str | None = None) -> None:
    """Create the items table if it doesn't exist. Idempotent."""
    with sqlite3.connect(_resolve_path(path)) as conn:
        conn.execute(_SCHEMA)


def is_seen(url: str) -> bool:
    """Return True if this URL has already been recorded."""
    with sqlite3.connect(_resolve_path()) as conn:
        row = conn.execute("SELECT 1 FROM items WHERE url = ?", (url,)).fetchone()
    return row is not None


def mark_seen(
    url: str,
    source: str,
    title: str,
    verdict: str,
    relevance: int | None,
) -> None:
    """
    UPSERT an item record. On conflict, refreshes verdict/relevance/last_action
    but preserves the original first_seen timestamp.
    """
    with sqlite3.connect(_resolve_path()) as conn:
        conn.execute(
            """
            INSERT INTO items (url, source, title, verdict, relevance, last_action)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(url) DO UPDATE SET
                source      = excluded.source,
                title       = excluded.title,
                verdict     = excluded.verdict,
                relevance   = excluded.relevance,
                last_action = CURRENT_TIMESTAMP
            """,
            (url, source, title, verdict, relevance),
        )


def get_recent(days: int = 7, verdict: str | None = None) -> list[dict[str, Any]]:
    """Return items first seen within the last N days, newest first."""
    query = (
        "SELECT url, source, title, first_seen, verdict, relevance, last_action "
        "FROM items "
        "WHERE first_seen >= datetime('now', ?)"
    )
    params: list[Any] = [f"-{days} days"]
    if verdict is not None:
        query += " AND verdict = ?"
        params.append(verdict)
    query += " ORDER BY first_seen DESC"

    with sqlite3.connect(_resolve_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _print_stats() -> None:
    with sqlite3.connect(_resolve_path()) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        verdict_rows = conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM items GROUP BY verdict"
        ).fetchall()
        recent = conn.execute(
            "SELECT title, source, first_seen FROM items "
            "ORDER BY first_seen DESC LIMIT 1"
        ).fetchone()

    counts = {(r["verdict"] or "null"): r["n"] for r in verdict_rows}
    expected = ["skip", "news_repost", "sourced_post", "pending"]
    parts = [f"{v}={counts.get(v, 0)}" for v in expected]
    # Surface any unexpected verdict values (incl. NULLs) so they don't hide
    extras = [f"{k}={v}" for k, v in counts.items() if k not in expected]

    print(f"Total items: {total}")
    print("Per-verdict counts: " + ", ".join(parts + extras))
    if recent is None:
        print("Most recent item: (none)")
    else:
        print(
            f"Most recent item: {recent['title']} "
            f"from {recent['source']} at {recent['first_seen']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="lib.store utilities")
    parser.add_argument(
        "--stats", action="store_true", help="Print database stats and exit"
    )
    args = parser.parse_args()

    init_db()
    if args.stats:
        _print_stats()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
