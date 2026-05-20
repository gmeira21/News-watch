"""
Claude-backed relevance filter.

judge_item() takes a normalized item dict from fetch.py and returns
{relevance, verdict, reasoning}. The function never raises — any failure
(network, malformed JSON, validation error) produces a "pending" verdict
with the error message captured in `reasoning`.

The prompt template lives in prompts/relevance.md and is loaded once at
import. Placeholders are substituted via str.replace rather than
str.format because the template embeds a literal JSON example whose
braces would collide with format-string syntax.
"""

import argparse
import json
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 200
VALID_VERDICTS = {"skip", "news_repost", "sourced_post"}

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "relevance.md"
PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")

load_dotenv()

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _render_prompt(item: dict) -> str:
    summary = item.get("summary") or "(no summary available)"
    return (
        PROMPT_TEMPLATE
        .replace("{source_name}", item["source"])
        .replace("{source_tags}", ", ".join(item.get("tags") or []))
        .replace("{source_category_hint}", item.get("category_hint") or "either")
        .replace("{title}", item["title"])
        .replace("{summary}", summary)
    )


def _extract_json(text: str) -> Any:
    """Parse model output as JSON, tolerating optional ```json fences."""
    s = text.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return json.loads(s)


def _validate(parsed: Any) -> tuple[int, str, str]:
    if not isinstance(parsed, dict):
        raise ValueError("response is not a JSON object")
    rel = parsed.get("relevance")
    # Reject bools explicitly — True/False are ints in Python.
    if not isinstance(rel, int) or isinstance(rel, bool) or not 1 <= rel <= 5:
        raise ValueError(f"invalid relevance: {rel!r}")
    verdict = parsed.get("verdict")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    reasoning = parsed.get("reasoning") or ""
    return rel, verdict, str(reasoning)


def judge_item(item: dict) -> dict:
    try:
        prompt = _render_prompt(item)
        msg = _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        parsed = _extract_json(text)
        rel, verdict, reasoning = _validate(parsed)
        return {"relevance": rel, "verdict": verdict, "reasoning": reasoning}
    except Exception as e:
        return {
            "relevance": None,
            "verdict": "pending",
            "reasoning": f"API error: {e}",
        }


def judge_batch(items: list[dict]) -> list[dict]:
    results: list[dict] = []
    counts = {"skip": 0, "news_repost": 0, "sourced_post": 0, "pending": 0}
    n = len(items)
    for i, item in enumerate(items, start=1):
        title = (item.get("title") or "")[:60]
        result = judge_item(item)
        verdict = result["verdict"]
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == "pending":
            print(
                f"[FILTER] {i}/{n}: {title} → FAILED ({result['reasoning']}) "
                f"— marked pending"
            )
        else:
            print(
                f"[FILTER] {i}/{n}: {title} → "
                f"score={result['relevance']} verdict={verdict}"
            )
        results.append(result)
    print(
        f"[FILTER] Done. skip={counts['skip']} "
        f"news_repost={counts['news_repost']} "
        f"sourced_post={counts['sourced_post']} "
        f"pending={counts['pending']}"
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="lib.filter — relevance judging")
    parser.add_argument(
        "--test",
        type=str,
        help="Judge a single fake item with this title",
    )
    args = parser.parse_args()

    if not args.test:
        parser.print_help()
        return

    item = {
        "source": "Naval News",
        "tags": ["naval", "usv"],
        "category_hint": "news_repost_candidate",
        "title": args.test,
        "summary": "(test invocation)",
    }
    result = judge_item(item)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
