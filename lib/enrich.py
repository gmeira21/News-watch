"""
PDF enrichment for sourced_post candidates.

After the relevance filter has tagged an item as a sourced_post, this
module tries to locate the citable PDF behind it. If a PDF is found we
set judgment['pdf_url']. If not, we downgrade the verdict to
news_repost so the digest still surfaces the item — just without a
report-grade citation. Pure HTTP/HTML — no LLM calls.
"""

import argparse
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "NavictusNewsWatch/0.1 (+https://github.com/gmeira21/News-watch)"
HEAD_TIMEOUT = 10.0
GET_TIMEOUT = 15.0
MAX_REDIRECTS = 3
MAX_CANDIDATES_TO_VERIFY = 3

REPORT_PHRASES = (
    "read the report",
    "download the report",
    "full report",
    "the report",
    "download pdf",
    "view pdf",
    "download the document",
)

_THEME_PATH_FRAGMENTS = (
    "/wp-content/themes/",
    "/sites/default/files/styles/",
)

# Hostname substrings that suggest a document-hosting CDN — used for the
# cross-domain check on <iframe>/<embed>/<object> candidates (which have
# no anchor text we can phrase-match against).
_DOC_HOST_HINTS = ("cdn", "files", "documents", "uploads", "media")


def _http_client(timeout: float) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
    )


def _is_pdf_response(resp: httpx.Response) -> bool:
    if not (200 <= resp.status_code < 300):
        return False
    return "pdf" in resp.headers.get("content-type", "").lower()


def _head_is_pdf(url: str) -> bool:
    try:
        with _http_client(HEAD_TIMEOUT) as client:
            resp = client.head(url)
        return _is_pdf_response(resp)
    except Exception:
        return False


def _phrase_match(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in REPORT_PHRASES)


def _looks_like_theme_asset(path: str) -> bool:
    return any(frag in path for frag in _THEME_PATH_FRAGMENTS)


def _discover_pdf(article_url: str) -> tuple[str | None, str | None]:
    """
    Fetch the article URL and try to locate a citable PDF behind it.

    Returns a (pdf_url, no_pdf_reason) tuple:
      - (url, None)                       → a PDF was discovered and HEAD-verified
      - (None, "html_native")              → no usable PDF candidates on the page
      - (None, "candidate_unverified")     → candidates existed but none HEAD-verified

    Network/parse errors propagate to the caller (enrich_item), which
    catches them and treats them as enrichment errors.
    """
    with _http_client(GET_TIMEOUT) as client:
        resp = client.get(article_url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    article_host = urlparse(article_url).netloc

    # (candidate_url, anchor_text, kind) — kind is "a" or "embed", which
    # determines how the cross-domain check is applied below.
    candidates: list[tuple[str, str, str]] = []

    # Pass A: anchors whose href path ends in .pdf
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if urlparse(href).path.lower().endswith(".pdf"):
            candidates.append(
                (urljoin(article_url, href), a.get_text(" ", strip=True), "a")
            )

    # Pass B: anchors whose visible text matches a report phrase —
    # only consulted when no direct .pdf hrefs were found.
    if not candidates:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            text = a.get_text(" ", strip=True)
            if _phrase_match(text):
                candidates.append((urljoin(article_url, href), text, "a"))

    # Pass C: iframe/embed/object tags whose src or data attribute points
    # at a PDF. Used by think-tank pages that mount the PDF in a viewer
    # rather than offering it as a download link.
    for tag_name, attr in (("iframe", "src"), ("embed", "src"), ("object", "data")):
        for t in soup.find_all(tag_name):
            val = t.get(attr)
            if not val:
                continue
            val = val.strip()
            if ".pdf" in val.lower():
                candidates.append((urljoin(article_url, val), "", "embed"))

    surviving: list[str] = []
    for cand_url, anchor_text, kind in candidates:
        parsed = urlparse(cand_url)
        if _looks_like_theme_asset(parsed.path):
            continue
        if parsed.netloc and parsed.netloc != article_host:
            if kind == "a":
                # Anchor candidates: cross-domain only if the link text
                # itself strongly suggests a report.
                if not _phrase_match(anchor_text):
                    continue
            else:
                # Embedded candidates have no anchor text; allow cross-
                # domain only when the hostname hints at a document CDN.
                host = parsed.netloc.lower()
                if not any(hint in host for hint in _DOC_HOST_HINTS):
                    print(
                        f"[ENRICH] discovery: skipping cross-domain embed "
                        f"{cand_url} (host '{host}' lacks doc-hosting hint)"
                    )
                    continue
        surviving.append(cand_url)

    if not surviving:
        return None, "html_native"

    for cand_url in surviving[:MAX_CANDIDATES_TO_VERIFY]:
        if _head_is_pdf(cand_url):
            return cand_url, None

    return None, "candidate_unverified"


def _append_suffix(judgment: dict, suffix: str) -> None:
    base = judgment.get("reasoning") or ""
    judgment["reasoning"] = (base + " " + suffix).strip()


def enrich_item(item: dict, judgment: dict) -> dict:
    """
    Try to attach a citable PDF URL to a sourced_post judgment.

    Policy:
      - Found and verified                → pdf_url set, verdict stays
                                            sourced_post, no no_pdf_reason
      - No candidates on the page          → pdf_url=None, no_pdf_reason=
                                            "html_native", verdict stays
                                            sourced_post (article URL is the
                                            citable source)
      - Candidates found but unverifiable  → no_pdf_reason="candidate_unverified"
      - Pre-set pdf_url didn't HEAD-verify → no_pdf_reason="preset_unverified"
                                            (do NOT fall through to discovery)
      - Exception during enrichment        → verdict downgrades to news_repost,
                                            no_pdf_reason="error: <msg>"
    Never raises.
    """
    if judgment.get("verdict") != "sourced_post":
        return judgment

    try:
        existing_pdf = item.get("pdf_url")
        if existing_pdf:
            if _head_is_pdf(existing_pdf):
                judgment["pdf_url"] = existing_pdf
            else:
                judgment["pdf_url"] = None
                judgment["no_pdf_reason"] = "preset_unverified"
            return judgment

        url, reason = _discover_pdf(item["url"])
        if url:
            judgment["pdf_url"] = url
        else:
            judgment["pdf_url"] = None
            judgment["no_pdf_reason"] = reason
        return judgment
    except Exception as e:
        print(
            f"[WARN] Enrichment failed for "
            f"{(item.get('title') or '<unknown>')[:60]}: {e}"
        )
        judgment["verdict"] = "news_repost"
        judgment["pdf_url"] = None
        judgment["no_pdf_reason"] = f"error: {e}"
        _append_suffix(judgment, "(downgraded: enrichment error)")
        return judgment


def _short(url: str | None, max_len: int = 60) -> str:
    if not url:
        return "None"
    if len(url) <= max_len:
        return url
    return url[: max_len - 1] + "…"


def enrich_batch(items: list[dict], judgments: list[dict]) -> list[dict]:
    if len(items) != len(judgments):
        raise ValueError("items and judgments must have the same length")

    n = len(items)
    counts = {"with_pdf": 0, "html_native": 0, "unverified": 0, "errors": 0}
    results: list[dict] = []

    for i, (item, judgment) in enumerate(zip(items, judgments), start=1):
        title = (item.get("title") or "")[:60]
        # Snapshot the input verdict — enrich_item may mutate it (error path).
        input_verdict = judgment.get("verdict")
        updated = enrich_item(item, judgment)
        results.append(updated)

        reason = updated.get("no_pdf_reason")
        if input_verdict == "sourced_post":
            if updated.get("pdf_url"):
                counts["with_pdf"] += 1
            elif reason == "html_native":
                counts["html_native"] += 1
            elif reason in ("candidate_unverified", "preset_unverified"):
                counts["unverified"] += 1
            elif reason and reason.startswith("error:"):
                counts["errors"] += 1
        # Non-sourced_post items pass through unchanged; no counter bump.

        print(
            f"[ENRICH] {i}/{n}: {title} → "
            f"pdf_url={_short(updated.get('pdf_url'))} "
            f"verdict={updated.get('verdict')} "
            f"reason={reason or 'n/a'}"
        )

    print(
        f"[ENRICH] Done. with_pdf={counts['with_pdf']} "
        f"html_native={counts['html_native']} "
        f"unverified={counts['unverified']} "
        f"errors={counts['errors']}"
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="lib.enrich — PDF discovery")
    parser.add_argument(
        "--url",
        type=str,
        help="Article URL to inspect for a citable PDF",
    )
    args = parser.parse_args()

    if not args.url:
        parser.print_help()
        return

    # If the URL itself looks like a direct PDF — by path suffix or by a
    # HEAD content-type check — route it through the "already-known"
    # branch of enrich_item. Otherwise the discovery path would GET the
    # URL and try to parse PDF bytes as HTML.
    url = args.url
    item: dict = {"url": url}
    if urlparse(url).path.lower().endswith(".pdf") or _head_is_pdf(url):
        item["pdf_url"] = url

    judgment: dict = {"verdict": "sourced_post", "reasoning": ""}
    result = enrich_item(item, judgment)
    pdf = result.get("pdf_url")
    if pdf:
        print(pdf)
    else:
        reason = result.get("no_pdf_reason") or "unknown"
        print(f"no PDF found (reason: {reason})")


if __name__ == "__main__":
    main()
