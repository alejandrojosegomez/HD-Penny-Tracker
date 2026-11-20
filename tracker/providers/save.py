"""Write an Observation into price_history. Used by every provider and by manual entry."""
from __future__ import annotations

import logging

from .base import Observation

log = logging.getLogger("tracker.providers")


def save_observation(obs: Observation, db=None) -> int:
    """Store one observation. Returns the price_history id. The store must exist in
    config/stores.yaml; the product is created if new."""
    from ..storage import get_storage
    db = db or get_storage()
    st = next((s for s in db.stores() if s["hd_store_id"] == obs.hd_store_id), None)
    if not st:
        raise ValueError(f"store {obs.hd_store_id} is not in config/stores.yaml")
    pid = db.upsert_product(obs.internet_number, **obs.product_fields())
    raw = {"source": obs.source, "observed_at": obs.observed_at, **{k: v for k, v in obs.extra.items() if v is not None}}
    row = db.add_price(pid, st["id"], obs.price, obs.original_price, obs.stock, obs.availability, raw=raw)
    log.info("%s @ %s: $%s (orig $%s) %s qty=%s [%s]", obs.internet_number, obs.hd_store_id, obs.price,
             obs.original_price, obs.availability, obs.stock, obs.source)
    return row
