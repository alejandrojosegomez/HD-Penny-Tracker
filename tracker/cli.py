"""python -m tracker <command>

  add <internet_number> [--category c] [--note n]   add a product to the watchlist
  price <internet#> <store> <price> [--was X] [--qty N]
                                                     save a price you saw (manual mode)
  scrape [--limit N]                                 scrape watchlist x stores once
  collect [--product N ...] [--sku S ...] [--store ID ...] [--provider P]
                                                     run one collection job now (default: everything)
  jobs [--limit N]                                   recent collection jobs
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
    # httpx logs every request URL at INFO, and Telegram's URL contains the bot token
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    _logging()
    ap = argparse.ArgumentParser(prog="tracker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add"); a.add_argument("internet_number"); a.add_argument("--category", default=""); a.add_argument("--note", default="")
    pr = sub.add_parser("price"); pr.add_argument("internet_number"); pr.add_argument("store"); pr.add_argument("price", type=float)
    pr.add_argument("--was", type=float, default=None, help="regular price (reuses the last one if omitted)")
    pr.add_argument("--qty", type=int, default=None, help="units on the shelf (0 = none)")
    pr.add_argument("--no-send", action="store_true")
    s = sub.add_parser("scrape"); s.add_argument("--limit", type=int, default=None)
    c = sub.add_parser("collect")
    c.add_argument("--product", action="append", default=[], help="internet number (repeatable)")
    c.add_argument("--sku", action="append", default=[], help="store SKU (repeatable)")
    c.add_argument("--store", action="append", default=[], help="hd_store_id (repeatable)")
    c.add_argument("--provider", default=None, help="provider name (default: PRICE_PROVIDER)")
    c.add_argument("--limit", type=int, default=None)
    c.add_argument("--no-detect", action="store_true", help="don't re-score after collecting")
    j = sub.add_parser("jobs"); j.add_argument("--limit", type=int, default=10)
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
    elif args.cmd == "price":
        from .manual import ManualPriceError, record_price
        try:
            r = record_price(args.internet_number, args.store, args.price, args.was, args.qty,
                             send_alerts=not args.no_send)
        except ManualPriceError as e:
            sys.exit(str(e))
        disc = f" ({r['discount_pct']:.0f}% off)" if r["discount_pct"] is not None else ""
        print(f"{r['name'] or r['internet_number']} @ {r['store_name']} #{r['hd_store_id']}: "
              f"${r['price']:.2f}{disc} -> score {r['score']}" + (f" | {'; '.join(r['reasons'])}" if r["reasons"] else ""))
    elif args.cmd == "scrape":
        if not settings.scrape_enabled:
            sys.exit("scraping is off (SCRAPE_ENABLED=false). Use `python -m tracker price ...` instead.")
        from .scraper import scrape_all
        print(scrape_all(limit=args.limit, trigger="cli"))
    elif args.cmd == "collect":
        from .collector import run_collection
        from .notify import send
        from .providers import get_provider
        provider = get_provider(args.provider)
        if provider.name == "hd_direct" and not settings.scrape_enabled:
            sys.exit("hd_direct is off (SCRAPE_ENABLED=false). Pick another provider with --provider.")
        res = run_collection(internet_numbers=args.product, skus=args.sku, stores=args.store,
                             provider=provider, trigger="cli", limit=args.limit,
                             notify=send)
        print(res)
        if not args.no_detect and res.get("saved"):
            from .detect import run
            print(run())
    elif args.cmd == "jobs":
        from .collector import jobs as cjobs
        from .storage import get_storage
        for r in cjobs.recent(get_storage(), args.limit):
            print(f"  #{r['id']:<4} {r['created_at']}  {r['trigger']:<9} {r['provider']:<12} {r['status']:<9} "
                  f"products={r['products_processed']} stores={r['stores_processed']} saved={r['saved']} "
                  f"rejected={r['rejected']} errors={r['errors']}  {r.get('detail') or ''}")
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
