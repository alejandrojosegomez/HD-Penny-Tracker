"""Walks the watchlist × stores and appends one price_history row per pair.

The loop itself lives in tracker/collector/engine.py; this module keeps the original entry
point (scrape_all) and the watchlist sync.

Prices come from the provider chosen with PRICE_PROVIDER (see tracker/providers/).
"""
from __future__ import annotations

import logging

from .notify import send
from .providers import get_provider
from .settings import read_watchlist
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


def scrape_all(limit: int | None = None, provider=None, trigger: str = "manual") -> dict:
    """Whole watchlist x every store, once. Runs through the collection engine
    (tracker/collector/), which records a collection job and enforces the request budget."""
    from .collector import run_collection
    # look send / sync up at call time so tests (and callers) can swap them
    return run_collection(provider=provider or get_provider(), trigger=trigger, limit=limit,
                          notify=lambda m: send(m), sync=sync_watchlist_to_db, run_name="scrape")
