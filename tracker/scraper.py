"""Walks the watchlist × stores and appends one price_history row per pair.

Prices come from the provider chosen with PRICE_PROVIDER (see tracker/providers/).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from .notify import send
from .providers import Status, get_provider
from .providers.save import save_observation
from .settings import read_watchlist, settings
from .storage import get_storage

log = logging.getLogger("tracker.scraper")


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


@contextmanager
def _scrape_lock():
    """One scrape at a time across processes/containers (worker schedule + dashboard button
    share data/). Yields False if another scrape is already running."""
    try:
        import fcntl
    except ImportError:          # Windows dev box: single process, nothing to coordinate
        yield True
        return
    path = settings.sqlite_path.parent / "scrape.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def scrape_all(limit: int | None = None, provider=None) -> dict:
    with _scrape_lock() as got:
        if not got:
            log.warning("another scrape is already running; skipping this one")
            return {"ok": 0, "fail": 0, "blocked": 0, "stopped": True, "skipped": "already running"}
        return _scrape_all(limit, provider)


def _scrape_all(limit: int | None = None, provider=None) -> dict:
    db = get_storage()
    run_id = db.start_run("scrape")
    sync_watchlist_to_db()
    products = db.products()
    if limit:
        products = products[:limit]
    stores = db.stores()
    provider = provider or get_provider()
    log.info("scraping %d products x %d stores with provider %s", len(products), len(stores), provider.name)

    counts = {s.value: 0 for s in Status}
    for p in products:
        for s in stores:
            res = provider.fetch(p["internet_number"], s)
            counts[res.status.value] += 1
            if res.status.stops_run:
                # a refusal ends the run: no retries, no extra requests until the next schedule
                msg = (f"HD Penny Tracker: {provider.name} answered {res.status.value} ({res.detail}) "
                       f"on {p['internet_number']} @ {s['hd_store_id']}. Stopping this run; "
                       f"{counts['ok']} saved. Manual prices still work.")
                log.error(msg)
                send(msg)
                db.finish_run(run_id, False, msg)
                return _summary(counts, stopped=True, provider=provider.name)
            if res.status is Status.OK:
                save_observation(res.observation, db)
            else:
                log.warning("%s @ %s: %s %s", p["internet_number"], s["hd_store_id"],
                            res.status.value, res.detail)
            provider.pause()

    out = _summary(counts, stopped=False, provider=provider.name)
    summary = (f"scrape done ({provider.name}): {out['ok']} ok, {out['fail']} failed, "
               f"{out['unavailable']} unavailable")
    db.finish_run(run_id, True, summary)
    log.info(summary)
    return out


def _summary(counts: dict, stopped: bool, provider: str) -> dict:
    return {"ok": counts["ok"],
            "fail": counts["error"] + counts["malformed"],
            "blocked": counts["access_denied"] + counts["rate_limited"],
            "unavailable": counts["unavailable"],
            "stopped": stopped, "provider": provider}
