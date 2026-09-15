"""
CLI: `python -m aade scrape` | `outreach` | `init-db`
"""

from __future__ import annotations

import argparse
import sys

from aade import database as db
from aade.config import DB_PATH, CONTACT_COOLDOWN_DAYS
from aade.pipeline import run_outreach_batch, run_scrape


def main() -> int:
    ap = argparse.ArgumentParser(description="Ann Arbor Digital Growth Engine")
    sub = ap.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Create SQLite schema under data/")
    p_init.set_defaults(_fn=cmd_init_db)

    p_sc = sub.add_parser("scrape", help="Scrape / fetch leads and upsert to SQLite")
    p_sc.add_argument(
        "--query",
        default="small businesses in Ann Arbor MI",
        help="Maps / Places text query (default: Ann Arbor businesses)",
    )
    p_sc.add_argument("--max", type=int, default=20, help="Max leads to collect")
    p_sc.set_defaults(_fn=cmd_scrape)

    p_or = sub.add_parser("outreach", help="Generate AI outreach for no-website leads (cooldown-aware)")
    p_or.add_argument("--limit", type=int, default=5, help="Max new contacts this run")
    p_or.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Skip the PNG landing page screenshot step",
    )
    p_or.set_defaults(_fn=cmd_outreach)

    args = ap.parse_args()
    return int(args._fn(args))


def cmd_init_db(_args: argparse.Namespace) -> int:
    with db.connect():
        pass
    print(f"OK: database ready at {DB_PATH}")
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    r = run_scrape(query=args.query, max_results=args.max)
    print(
        f"Scrape done: {r.new_rows} new place_id rows, "
        f"{r.batch_missing_website} of {r.batch_size} fetched with no public website in this batch.",
    )
    return 0


def cmd_outreach(args: argparse.Namespace) -> int:
    r = run_outreach_batch(
        limit=args.limit,
        with_screenshots=not args.no_screenshots,
    )
    print(
        f"Outreach: {r.processed} sent, {r.skipped_cooldown} skipped ("
        f"{CONTACT_COOLDOWN_DAYS}-day cooldown), {r.skipped_has_website} has website."
    )
    for p in r.paths_screenshot:
        print(f"  screenshot: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
