"""
Navictus News Watch — orchestrator.

Wires fetch → store-dedup → filter → enrich → store-persist → deliver
into a single pipeline run. Invoked by GitHub Actions on a schedule
(Mon/Wed/Fri 07:00 UTC). This file MUST NOT contain business logic —
filtering rules, formatting, scraping all live in the lib modules.
"""

import argparse
import sys
import time

from dotenv import load_dotenv

from lib import deliver as deliver_mod
from lib import enrich as enrich_mod
from lib import fetch as fetch_mod
from lib import filter as filter_mod
from lib import store as store_mod

MAX_MANUAL_CHECKS_IN_DIGEST = 3


def _full_pipeline(limit: int | None, dry_run: bool, send_empty: bool) -> int:
    started = time.perf_counter()

    store_mod.init_db()

    items = fetch_mod.fetch_all()
    sources_with_items = {i.get("source") for i in items if i.get("source")}
    print(f"[RUN] Fetched {len(items)} items from {len(sources_with_items)} sources")

    new_items = [i for i in items if not store_mod.is_seen(i["url"])]
    already_seen = len(items) - len(new_items)
    print(f"[RUN] {len(new_items)} new items ({already_seen} already seen, skipping)")

    if limit is not None and limit < len(new_items):
        print(f"[RUN] --limit {limit}: truncating from {len(new_items)} to {limit}")
        new_items = new_items[:limit]

    if not new_items:
        print("[RUN] No new items to process")
        delivered = 0
        delivery_failed = False
        if send_empty and not dry_run:
            ok = deliver_mod.deliver([], [])
            if ok:
                delivered = 1
            else:
                print("[RUN] Delivery failed (empty run — no judgments to persist)")
                delivery_failed = True
        elapsed = time.perf_counter() - started
        print(
            f"[RUN] Done. fetched={len(items)} new=0 "
            f"delivered={delivered} duration={elapsed:.1f}s"
        )
        return 1 if delivery_failed else 0

    judgments = filter_mod.judge_batch(new_items)
    enriched_judgments = enrich_mod.enrich_batch(new_items, judgments)

    # Deliverables = high-relevance verdicts, plus up to N manual-check
    # reminders. Manual-checks bypass the verdict gate because the
    # relevance filter scores them low but the digest still surfaces them.
    deliverable_items: list[dict] = []
    deliverable_judgments: list[dict] = []
    seen_urls: set[str] = set()

    for item, judgment in zip(new_items, enriched_judgments):
        if judgment.get("verdict") in ("news_repost", "sourced_post"):
            deliverable_items.append(item)
            deliverable_judgments.append(judgment)
            seen_urls.add(item["url"])

    manual_added = 0
    for item, judgment in zip(new_items, enriched_judgments):
        if item.get("source_type") != "manual_check":
            continue
        if item["url"] in seen_urls:
            continue
        if manual_added >= MAX_MANUAL_CHECKS_IN_DIGEST:
            break
        deliverable_items.append(item)
        deliverable_judgments.append(judgment)
        seen_urls.add(item["url"])
        manual_added += 1

    print(f"[RUN] {len(deliverable_items)} items will be delivered")

    # Dry-run: preview the digest, skip both delivery and store writes.
    if dry_run:
        msg = deliver_mod.format_digest(deliverable_items, deliverable_judgments)
        print("\n[RUN] --dry-run: digest preview follows\n")
        print(msg)
        print(
            f"\n[RUN] --dry-run: NOT sending to Telegram; "
            f"would have stored {len(new_items)} judgments"
        )
        elapsed = time.perf_counter() - started
        print(
            f"[RUN] Done. fetched={len(items)} new={len(new_items)} "
            f"delivered=0 duration={elapsed:.1f}s"
        )
        return 0

    # Deliver first, then persist. If delivery fails, leave the items
    # unseen so the next scheduled run re-fetches and re-judges them —
    # one wasted Haiku batch (~$0.05) beats losing a digest entirely.
    delivered = 0
    delivery_failed = False
    should_attempt_delivery = bool(deliverable_items) or send_empty

    if should_attempt_delivery:
        ok = deliver_mod.deliver(deliverable_items, deliverable_judgments)
        if ok:
            delivered = len(deliverable_items)
        else:
            print(
                "[RUN] Delivery failed — judgments not persisted, "
                "will retry next run"
            )
            delivery_failed = True
    else:
        print(
            "[RUN] no deliverable items; skipping Telegram "
            "(use --send-empty to override)"
        )

    if not delivery_failed:
        for item, judgment in zip(new_items, enriched_judgments):
            store_mod.mark_seen(
                url=item["url"],
                source=item["source"],
                title=item["title"],
                verdict=judgment["verdict"],
                relevance=judgment.get("relevance"),
            )
        print(f"[RUN] Stored {len(new_items)} judgments to seen.db")

    elapsed = time.perf_counter() - started
    print(
        f"[RUN] Done. fetched={len(items)} new={len(new_items)} "
        f"delivered={delivered} duration={elapsed:.1f}s"
    )
    return 1 if delivery_failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Navictus News Watch — pipeline orchestrator"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but print the digest instead of sending; "
             "do NOT write to the store",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N items after dedup (for testing)",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print store statistics and exit (no fetch, no judge, no send)",
    )
    parser.add_argument(
        "--send-empty",
        action="store_true",
        help="Send a quiet-run digest to Telegram even when there are zero "
             "new items (default: skip Telegram on empty runs)",
    )
    args = parser.parse_args()

    load_dotenv()

    try:
        if args.stats:
            store_mod.init_db()
            store_mod._print_stats()
            return 0

        return _full_pipeline(
            limit=args.limit,
            dry_run=args.dry_run,
            send_empty=args.send_empty,
        )
    except Exception as e:
        print(f"[RUN] FATAL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
