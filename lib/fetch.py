"""
Source fetcher for the news-watch agent.

Reads sources.yaml, dispatches each entry to the appropriate handler
(RSS via feedparser, custom HTML scrapers, or weekly manual-check
reminders), and returns a flat list of normalized item dicts.

Failures in any single source are logged as warnings — fetch_all
always returns a list, never raises.
"""

from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin

import feedparser
import httpx
import yaml
from bs4 import BeautifulSoup

USER_AGENT = "NavictusNewsWatch/0.1 (+https://github.com/gmeira21/News-watch)"
HTTP_TIMEOUT = 15.0
HTTP_MAX_REDIRECTS = 3

SCRAPERS: dict[str, Callable[[dict], list[dict]]] = {}


def register_scraper(name: str) -> Callable[[Callable[[dict], list[dict]]], Callable[[dict], list[dict]]]:
    """Decorator that adds a scraper function to the SCRAPERS registry."""
    def decorator(fn: Callable[[dict], list[dict]]) -> Callable[[dict], list[dict]]:
        SCRAPERS[name] = fn
        return fn
    return decorator


def _http_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        max_redirects=HTTP_MAX_REDIRECTS,
    )


def _struct_time_to_iso(st: Any) -> str | None:
    """Convert feedparser's time.struct_time (UTC) to an ISO 8601 string."""
    if st is None:
        return None
    try:
        return datetime(*st[:6], tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _fetch_rss(source: dict) -> list[dict]:
    parsed = feedparser.parse(
        source["url"],
        request_headers={"User-Agent": USER_AGENT},
    )
    if parsed.bozo and not parsed.entries:
        # Hard parse failure with no recoverable entries — treat as a fetch error.
        raise RuntimeError(f"feed parse failed: {parsed.bozo_exception!r}")

    items: list[dict] = []
    for entry in parsed.entries:
        url = entry.get("link")
        if not url:
            continue
        items.append({
            "url": url,
            "title": (entry.get("title") or "").strip(),
            "summary": (entry.get("summary") or "").strip(),
            "published": _struct_time_to_iso(entry.get("published_parsed")
                                             or entry.get("updated_parsed")),
            "source": source["name"],
            "source_type": "rss",
            "tags": list(source.get("tags") or []),
            "category_hint": source.get("category_hint", ""),
        })
    return items


def _fetch_scrape(source: dict) -> list[dict]:
    scraper = SCRAPERS.get(source["name"])
    if scraper is None:
        # TODO: implement scrapers for the remaining scrape-typed sources in
        # sources.yaml (NATO MARCOM, NATO ACT, EDA, EEAS, CSIS, Atlantic Council,
        # IISS, ICPC, UNCTAD, Janes OSINT, Shephard Media, DN Defesa, Observador
        # Defesa). Add each as a @register_scraper function.
        print(f"[WARN] No scraper registered for '{source['name']}' — skipping")
        return []
    return scraper(source)


def _fetch_manual_check(source: dict) -> list[dict]:
    """
    Emit a single weekly reminder item. The URL gets a `#manual-<ISO-week>`
    suffix so the store layer dedupes it for the rest of the week without
    fetch.py needing to know about the store.
    """
    today = datetime.now(timezone.utc)
    iso_year, iso_week, _ = today.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    return [{
        "url": f"{source['url']}#manual-{week_key}",
        "title": f"Manual check reminder: {source['name']}",
        "summary": (source.get("notes") or "").strip(),
        "published": today.date().isoformat(),
        "source": source["name"],
        "source_type": "manual_check",
        "tags": list(source.get("tags") or []),
        "category_hint": source.get("category_hint", ""),
    }]


# ----------------------------------------------------------------------------
# Scrapers
# ----------------------------------------------------------------------------

@register_scraper("Marinha Portuguesa — Notícias")
def _scrape_marinha_pt(source: dict) -> list[dict]:
    """
    Marinha Portuguesa news index. Each item on the page is an
    <a class="mediacenter-item ... noticia"> card with the title in the
    `title` attribute and a description embedded in `data-footer`.
    """
    with _http_client() as client:
        resp = client.get(source["url"])
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    items: list[dict] = []
    seen_urls: set[str] = set()
    for a in soup.select("a.noticia"):
        href = a.get("href")
        if not href:
            continue
        url = urljoin(source["url"], href)
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = (a.get("title") or a.get_text(strip=True) or "").strip()

        # data-footer contains a small HTML fragment with a .lightbox-desc span
        summary = ""
        data_footer = a.get("data-footer")
        if data_footer:
            footer_soup = BeautifulSoup(data_footer, "lxml")
            desc = footer_soup.select_one(".lightbox-desc")
            if desc:
                summary = desc.get_text(strip=True)

        items.append({
            "url": url,
            "title": title,
            "summary": summary,
            "published": None,
            "source": source["name"],
            "source_type": "scrape",
            "tags": list(source.get("tags") or []),
            "category_hint": source.get("category_hint", ""),
        })
    return items


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------

_DISPATCH: dict[str, Callable[[dict], list[dict]]] = {
    "rss": _fetch_rss,
    "scrape": _fetch_scrape,
    "manual_check": _fetch_manual_check,
}


def fetch_all(sources_yaml_path: str = "sources.yaml") -> list[dict]:
    with open(sources_yaml_path, encoding="utf-8") as f:
        sources = yaml.safe_load(f) or []

    print(f"Fetching {len(sources)} sources from {sources_yaml_path}")

    all_items: list[dict] = []
    for source in sources:
        name = source.get("name", "<unnamed>")
        src_type = source.get("type", "")
        handler = _DISPATCH.get(src_type)
        if handler is None:
            print(f"[WARN] Unknown source type '{src_type}' for {name} — skipping")
            continue

        print(f"Fetching {name} ({src_type})...")
        try:
            items = handler(source)
        except Exception as e:
            print(f"[WARN] Failed to fetch {name}: {e}")
            continue

        print(f"  → got {len(items)} items")
        all_items.extend(items)

    return all_items


def main() -> None:
    items = fetch_all()
    print(f"\nFetched {len(items)} items total:")
    counts: dict[str, int] = {}
    for item in items:
        counts[item["source"]] = counts.get(item["source"], 0) + 1
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {name}: {n}")


if __name__ == "__main__":
    main()
