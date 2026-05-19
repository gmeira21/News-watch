"""
Telegram digest formatting and delivery.

Takes the (item, judgment) pairs produced by filter + enrich, lays
them out as a MarkdownV2-formatted digest, and posts to a Telegram
chat via the Bot API. Pure formatting + HTTP — no LLM calls.
"""

import argparse
import os
import re
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

TELEGRAM_API = "https://api.telegram.org"
SEND_TIMEOUT = 15.0
# Telegram's hard cap is 4096 chars; we aim a little under to leave buffer.
TARGET_LEN = 4000

_MD_BODY_SPECIAL = "_*[]()~`>#+-=|{}.!"


def _escape_md(text: str) -> str:
    """Escape MarkdownV2 body specials with a leading backslash."""
    return "".join("\\" + c if c in _MD_BODY_SPECIAL else c for c in text)


def _escape_md_url(url: str) -> str:
    """Inside an inline-link URL only ')' and '\\' need escaping."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


_LEADING_BRACKET_RE = re.compile(r"^\[[^\]]+\]\s+")


def _pluralize(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _strip_leading_category(title: str) -> str:
    """Defensively remove a leading [Category] prefix from a title."""
    return _LEADING_BRACKET_RE.sub("", title).strip()


def short_source(source: str) -> str:
    """Normalize source names for compact display in digest lines."""
    s = source.split(" — ")[0].strip()
    paren = s.find(" (")
    if paren >= 0:
        s = s[:paren].strip()
    return s or source


# ---------------------------------------------------------------------------
# Grouping + rendering
# ---------------------------------------------------------------------------

def _sort_key(pair: tuple) -> tuple:
    item, judgment = pair
    return (-(judgment.get("relevance") or 0), item.get("source", ""))


def _group(items: list[dict], judgments: list[dict]) -> tuple[list, list, list, list]:
    sourced: list[tuple] = []
    news_repost: list[tuple] = []
    manual: list[tuple] = []
    borderline: list[tuple] = []
    for item, judgment in zip(items, judgments):
        if item.get("source_type") == "manual_check":
            manual.append((item, judgment))
            continue
        verdict = judgment.get("verdict")
        if verdict == "sourced_post":
            sourced.append((item, judgment))
        elif verdict == "news_repost":
            news_repost.append((item, judgment))
        elif judgment.get("relevance") == 3:
            borderline.append((item, judgment))
    sourced.sort(key=_sort_key)
    news_repost.sort(key=_sort_key)
    manual.sort(key=lambda p: p[0].get("source", ""))
    borderline.sort(key=_sort_key)
    return sourced, news_repost, manual, borderline


def _render_line(item: dict, judgment: dict, show_pdf: bool = False) -> str:
    title = _strip_leading_category(item.get("title") or "")
    title_e = _escape_md(title)
    url_e = _escape_md_url(item.get("url") or "")
    source_e = _escape_md(short_source(item.get("source") or ""))
    line = f"• [{title_e}]({url_e}) — {source_e}"
    if show_pdf:
        pdf_url = judgment.get("pdf_url")
        if pdf_url:
            line += f"  📄 [PDF]({_escape_md_url(pdf_url)})"
    return line


def _render_section(
    title_text: str,
    pairs: list[tuple],
    cap: int,
    show_pdf: bool = False,
) -> str:
    if not pairs:
        return ""
    lines = [f"*{_escape_md(title_text)}*", ""]
    for pair in pairs[:cap]:
        item, judgment = pair
        lines.append(_render_line(item, judgment, show_pdf=show_pdf))
    if len(pairs) > cap:
        extra = len(pairs) - cap
        lines.append(_escape_md(f"(+{extra} more — see full log)"))
    return "\n".join(lines)


def _build_digest_body(
    grouped: tuple[list, list, list, list],
    cap: int,
    include_borderline: bool,
    run_timestamp: datetime,
) -> str:
    sourced, news_repost, manual, borderline = grouped
    date_str = run_timestamp.strftime("%d %b %Y")
    header = f"*Navictus News Watch* — {date_str}"
    summary = (
        f"{_pluralize(len(sourced), 'sourced', 'sourced')} • "
        f"{_pluralize(len(news_repost), 'to repost', 'to repost')} • "
        f"{_pluralize(len(manual), 'manual check', 'manual checks')}"
    )
    blocks = [header, summary]

    s_sourced = _render_section("Sourced posts", sourced, cap, show_pdf=True)
    s_news = _render_section("News to repost", news_repost, cap)
    s_manual = _render_section("Manual checks", manual, cap)
    for s in (s_sourced, s_news, s_manual):
        if s:
            blocks.append(s)

    # Borderline only appears as a quiet-day fallback when the three
    # primary sections combined have fewer than 3 items to show.
    shown_above = (
        min(len(sourced), cap) + min(len(news_repost), cap) + min(len(manual), cap)
    )
    if include_borderline and shown_above < 3 and borderline:
        s_border = _render_section("Borderline (rated 3)", borderline, 3)
        if s_border:
            blocks.append(s_border)

    return "\n\n".join(blocks)


def format_digest(
    items: list[dict],
    judgments: list[dict],
    run_timestamp: datetime | None = None,
) -> str:
    if run_timestamp is None:
        run_timestamp = datetime.now(timezone.utc)
    date_str = run_timestamp.strftime("%d %b %Y")

    grouped = _group(items, judgments)
    sourced, news_repost, manual, borderline = grouped

    # Empty digest: nothing in any of the four buckets.
    if not (sourced or news_repost or manual or borderline):
        body = _escape_md("Quiet day — no high-relevance items found in this run.")
        return f"*Navictus News Watch* — {date_str}\n\n{body}"

    # Progressive fallback if we blow the 4000-char budget.
    for cap, with_border in ((8, True), (5, True), (5, False)):
        msg = _build_digest_body(grouped, cap, with_border, run_timestamp)
        if len(msg) <= TARGET_LEN:
            return msg

    print("[DELIVER] WARNING: digest still over length budget after fallbacks; truncating")
    msg = _build_digest_body(grouped, 5, False, run_timestamp)
    # Truncate at the last newline boundary that fits, then append a note.
    truncated = msg[:3900].rsplit("\n", 1)[0]
    suffix = _escape_md(
        "(+truncated; raise per-section cap or split into multiple messages)"
    )
    return f"{truncated}\n\n{suffix}"


# ---------------------------------------------------------------------------
# Telegram transport
# ---------------------------------------------------------------------------

def _strip_markdown(text: str) -> str:
    """Best-effort plain-text version for the fallback retry."""
    # [text](url) → text url
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", text)
    # Un-escape any \X back to X
    text = re.sub(r"\\(.)", r"\1", text)
    # Drop remaining bold markers
    text = text.replace("*", "")
    return text


def send_to_telegram(message: str, chat_id: str, bot_token: str) -> dict:
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    try:
        with httpx.Client(timeout=SEND_TIMEOUT) as client:
            resp = client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "MarkdownV2",
                },
            )
            data = resp.json()

            if (
                resp.status_code == 400
                and not data.get("ok")
                and "parse" in str(data.get("description", "")).lower()
            ):
                # Markdown parser rejected our message — retry as plain text.
                plain = _strip_markdown(message)
                resp2 = client.post(
                    url,
                    json={"chat_id": chat_id, "text": plain},
                )
                data2 = resp2.json()
                if data2.get("ok"):
                    print(
                        f"[DELIVER] Sent {len(plain)} chars to chat {chat_id} "
                        f"(plain-text fallback after MarkdownV2 parse error)"
                    )
                    return data2
                err = data2.get("description", "unknown error")
                print(f"[DELIVER] FAILED to send: {err}")
                return {"ok": False, "error": err}

            if data.get("ok"):
                print(f"[DELIVER] Sent {len(message)} chars to chat {chat_id}")
                return data

            err = data.get("description", "unknown error")
            print(f"[DELIVER] FAILED to send: {err}")
            return {"ok": False, "error": err}
    except Exception as e:
        print(f"[DELIVER] FAILED to send: {e}")
        return {"ok": False, "error": str(e)}


def deliver(items: list[dict], judgments: list[dict]) -> bool:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(
            "[DELIVER] FAILED to send: "
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in environment"
        )
        return False

    msg = format_digest(items, judgments)
    result = send_to_telegram(msg, chat_id, token)
    ok = bool(result.get("ok"))
    print(f"[DELIVER] deliver() result: {'ok' if ok else 'failed'}")
    return ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _make_fake_data() -> tuple[list[dict], list[dict]]:
    """5 fake items: 2 sourced_post (with PDF), 2 news_repost, 1 manual_check."""
    items = [
        {
            "url": "https://www.iscpc.org/documents/?id=4266",
            "title": "Submarine cables and marine biodiversity",
            "source": "ICPC (International Cable Protection Committee)",
            "source_type": "scrape",
            "tags": ["undersea_cables", "infrastructure", "security"],
            "category_hint": "sourced_post_candidate",
            "summary": "ICPC/UNEP-WCMC report on submarine cable installation impacts.",
            "published": "2026-04-15T00:00:00+00:00",
            "pdf_url": "https://www.iscpc.org/documents/?id=4266",
        },
        {
            "url": "https://eda.europa.eu/news-and-events/news/european-combat-vessel-update",
            "title": "European Combat Vessel programme: 2026 status update",
            "source": "European Defence Agency (EDA)",
            "source_type": "scrape",
            "tags": ["eu", "naval", "procurement"],
            "category_hint": "sourced_post_candidate",
            "summary": "EDA update on the ECV programme — autonomy modules, partner nations.",
            "published": "2026-05-01T00:00:00+00:00",
        },
        {
            "url": "https://www.navalnews.com/royal-navy-dynamic-messenger-mine-countermeasures-scotland/",
            "title": "Royal Navy trials NATO Dynamic Messenger autonomous mine countermeasures off Scottish coast",
            "source": "Naval News",
            "source_type": "rss",
            "tags": ["naval", "usv", "autonomy"],
            "category_hint": "news_repost_candidate",
            "summary": "Royal Navy autonomous MCM USVs operated alongside NATO partners during Dynamic Messenger 2026 trials.",
            "published": "2026-05-18T10:00:00+00:00",
        },
        {
            "url": "https://news.usni.org/usn-port-security-usv-contract/",
            "title": "USN awards $40M contract for autonomous port-security USVs",
            "source": "USNI News",
            "source_type": "rss",
            "tags": ["naval", "usv", "procurement"],
            "category_hint": "news_repost_candidate",
            "summary": "Contract awarded for fleet of port-security USVs.",
            "published": "2026-05-17T14:00:00+00:00",
        },
        {
            "url": "https://www.linkedin.com/company/naval-news/#manual-2026-W20",
            "title": "Manual check reminder: LinkedIn — Naval News company page",
            "source": "LinkedIn — Naval News company page",
            "source_type": "manual_check",
            "tags": ["linkedin", "naval", "usv"],
            "category_hint": "news_repost_candidate",
            "summary": "Reminder only. The agent can't scrape LinkedIn safely.",
            "published": "2026-05-19",
        },
    ]
    judgments = [
        {
            "relevance": 5,
            "verdict": "sourced_post",
            "reasoning": "Directly relevant to undersea-cable protection use case.",
            "pdf_url": "https://www.iscpc.org/documents/?id=4266",
        },
        {
            "relevance": 4,
            "verdict": "sourced_post",
            "reasoning": "EU programme update with autonomy implications.",
            "pdf_url": None,
            "no_pdf_reason": "html_native",
        },
        {
            "relevance": 5,
            "verdict": "news_repost",
            "reasoning": "USV + NATO — core Navictus narrative.",
        },
        {
            "relevance": 4,
            "verdict": "news_repost",
            "reasoning": "Port-security USV procurement signal.",
        },
        {
            "relevance": None,
            "verdict": "pending",
            "reasoning": "Manual-check passthrough.",
        },
    ]
    return items, judgments


def _send_raw(message: str) -> None:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[DELIVER] FAILED: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        return
    try:
        with httpx.Client(timeout=SEND_TIMEOUT) as client:
            resp = client.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
            )
        data = resp.json()
        if data.get("ok"):
            print(f"[DELIVER] Sent {len(message)} chars to chat {chat_id}")
        else:
            print(f"[DELIVER] FAILED: {data.get('description', 'unknown error')}")
    except Exception as e:
        print(f"[DELIVER] FAILED: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="lib.deliver — Telegram digest tools")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Print a fake digest to stdout (no send)",
    )
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="Print a fake digest and send it to the configured Telegram chat",
    )
    parser.add_argument(
        "--send-message",
        type=str,
        help="Send a single raw plain-text message (bot connection sanity check)",
    )
    args = parser.parse_args()

    if args.send_message is not None:
        _send_raw(args.send_message)
        return

    if args.test or args.send_test:
        items, judgments = _make_fake_data()
        msg = format_digest(items, judgments)
        print(msg)
        if args.send_test:
            load_dotenv()
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                print("[DELIVER] cannot send: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
                return
            send_to_telegram(msg, chat_id, token)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
