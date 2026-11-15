"""python -m tracker <command>

  add <internet_number> [--category c] [--note n]   add a product to the watchlist
  scrape [--limit N]                                 scrape watchlist x stores once
  detect [--no-send]                                 score history and create/send alerts
  community                                          refresh community penny lists
  run                                                start the scheduler (worker)
  serve                                              start the dashboard
  test-telegram                                      send a test message
  session                                            force a fresh browser session
  status                                             last runs + counts
"""
from __future__ import annotations

import argparse
import logging
import sys

from .settings import DATA_DIR, append_watchlist, settings


def _logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(DATA_DIR / "tracker.log")],
    )


def main() -> None:
    _logging()
    ap = argparse.ArgumentParser(prog="tracker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add"); a.add_argument("internet_number"); a.add_argument("--category", default=""); a.add_argument("--note", default="")
    s = sub.add_parser("scrape"); s.add_argument("--limit", type=int, default=None)
    d = sub.add_parser("detect"); d.add_argument("--no-send", action="store_true")
    sub.add_parser("community"); sub.add_parser("run"); sub.add_parser("serve")
    sub.add_parser("test-telegram"); sub.add_parser("session"); sub.add_parser("status")
    args = ap.parse_args()

    if args.cmd == "add":
        n = args.internet_number.strip()
        if not (n.isdigit() and len(n) in (9, 10)):
            sys.exit("internet number must be 9 or 10 digits (the number at the end of the product URL)")
        from .storage import get_storage
        added = append_watchlist(n, args.category, args.note, "manual")
        get_storage().upsert_product(n, category=args.category or None, source="manual")
        print("added" if added else "already on the watchlist")
    elif args.cmd == "scrape":
        from .scraper import scrape_all
        print(scrape_all(limit=args.limit))
    elif args.cmd == "detect":
        from .detect import run
        print(run(send_alerts=not args.no_send))
    elif args.cmd == "community":
        from .community import refresh
        print(refresh())
    elif args.cmd == "run":
        from .scheduler import main as run_sched
        run_sched()
    elif args.cmd == "serve":
        from .web import main as serve
        serve()
    elif args.cmd == "test-telegram":
        from .notify import test_message
        print("sent" if test_message() else "failed (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    elif args.cmd == "session":
        from .session import get_session
        s = get_session(force=True)
        print(f"session ok: {len(s.cookies)} cookies, UA={s.user_agent[:60]}...")
    elif args.cmd == "status":
        from .storage import get_storage
        db = get_storage()
        print(f"mode={settings.database_mode} stores={len(db.stores())} products={len(db.products())}")
        for r in db.last_runs(10):
            print(f"  {r['started_at']}  {r['job']:<10} ok={r['ok']}  {r.get('detail') or ''}")
