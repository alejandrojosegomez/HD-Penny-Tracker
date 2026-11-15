"""Walks the watchlist × stores and appends one price_history row per pair."""
from __future__ import annotations

import logging
import time

from .hd_client import Blocked, HDClient
from .notify import send
from .settings import read_watchlist, settings
from .storage import get_storage

log = logging.getLogger("tracker.scraper")

MAX_BLOCKS = 3          # consecutive blocks before we give up this run
BACKOFF_SECONDS = 300   # wait after a block before trying again


def sync_watchlist_to_db() -> int:
    """Make sure every watchlist row exists as a product. Returns number created."""
    db = get_storage()
    created = 0
    for row in read_watchlist():
        if not db.product_by_internet_number(row["internet_number"]):
            db.upsert_product(row["internet_number"], category=row.get("category") or None,
                              source=row.get("source") or "manual")
            created += 1
    return created


def scrape_all(limit: int | None = None) -> dict:
    db = get_storage()
    run_id = db.start_run("scrape")
    sync_watchlist_to_db()
    products = db.products()
    if limit:
        products = products[:limit]
    stores = db.stores()
    log.info("scraping %d products x %d stores", len(products), len(stores))

    client = HDClient()
    ok = fail = blocked = 0
    for p in products:
        for s in stores:
            try:
                data = client.fetch(p["internet_number"], s["hd_store_id"], s.get("zip") or "")
            except Blocked as e:
                blocked += 1
                log.warning("blocked (%s) on %s @ %s", e, p["internet_number"], s["hd_store_id"])
                if client.blocks_in_a_row >= MAX_BLOCKS:
                    msg = (f"HD Penny Tracker: blocked {MAX_BLOCKS} times in a row, stopping this run. "
                           f"Done {ok} ok / {fail} failed. Will retry next schedule.")
                    log.error(msg)
                    send(msg)
                    db.finish_run(run_id, False, msg)
                    return {"ok": ok, "fail": fail, "blocked": blocked, "stopped": True}
                time.sleep(BACKOFF_SECONDS)
                client.refresh_session()
                continue
            except Exception as e:
                fail += 1
                log.exception("fetch failed for %s @ %s: %s", p["internet_number"], s["hd_store_id"], e)
                client._sleep()
                continue

            db.upsert_product(
                p["internet_number"],
                sku=data.get("sku"), upc=data.get("upc"), model=data.get("model"), brand=data.get("brand"),
                name=data.get("name"), description=data.get("description"),
                image_url=data.get("image_url"), product_url=data.get("product_url"),
            )
            db.add_price(p["id"], s["id"], data.get("price"), data.get("original_price"),
                         data.get("stock"), data.get("availability"),
                         raw={"source": data.get("raw_source"), "discontinued": data.get("discontinued")})
            ok += 1
            log.info("%s @ %s: $%s (orig $%s) %s qty=%s", p["internet_number"], s["hd_store_id"],
                     data.get("price"), data.get("original_price"), data.get("availability"), data.get("stock"))
            client._sleep()

    summary = f"scrape done: {ok} ok, {fail} failed, {blocked} blocked"
    db.finish_run(run_id, True, summary)
    log.info(summary)
    return {"ok": ok, "fail": fail, "blocked": blocked, "stopped": False}
