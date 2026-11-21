"""Write an Observation into price_history. Used by every provider, the collector, the
/v1 API and manual entry: this is the only place price rows are written."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .base import Observation

log = logging.getLogger("tracker.providers")


@dataclass
class SaveResult:
    row_id: int
    created: bool                       # False = identical observation was already stored
    price_changed: bool                 # price differs from the previous row for this product/store
    previous_price: Optional[float] = None


def save_observation(obs: Observation, db=None, details: bool = False):
    """Store one observation. Returns the price_history id (or a SaveResult with details=True).

    The store must exist in config/stores.yaml; the product is created if new. Sending the
    same observation again (same source, observed_at and values) does not add a second row.
    """
    from ..storage import get_storage
    db = db or get_storage()
    st = next((s for s in db.stores() if s["hd_store_id"] == obs.hd_store_id), None)
    if not st:
        raise ValueError(f"store {obs.hd_store_id} is not in config/stores.yaml")
    pid = db.upsert_product(obs.internet_number, **obs.product_fields())

    existing = db.find_observation(pid, st["id"], obs.source, obs.observed_at, obs.price,
                                   obs.original_price, obs.stock, obs.availability)
    if existing:
        log.info("%s @ %s: duplicate of row %s [%s], not saved again", obs.internet_number,
                 obs.hd_store_id, existing, obs.source)
        res = SaveResult(existing, created=False, price_changed=False)
        return res if details else existing

    prev = db.last_price_row(pid, st["id"])
    prev_price = prev["price"] if prev else None
    changed = prev is not None and prev_price != obs.price

    raw = {"source": obs.source, "observed_at": obs.observed_at, **{k: v for k, v in obs.extra.items() if v is not None}}
    row = db.add_price(pid, st["id"], obs.price, obs.original_price, obs.stock, obs.availability, raw=raw)
    log.info("%s @ %s: $%s (orig $%s) %s qty=%s [%s]%s", obs.internet_number, obs.hd_store_id, obs.price,
             obs.original_price, obs.availability, obs.stock, obs.source,
             f" price changed from ${prev_price}" if changed else "")
    res = SaveResult(row, created=True, price_changed=changed, previous_price=prev_price)
    return res if details else row
