"""Short-lived cache of provider answers, used only for providers with cache_allowed=True.

A cache hit is not a new observation: it is the same answer (same observed_at), so saving
it again is a no-op thanks to the duplicate guard in providers.save.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..providers.base import Observation

UTC = timezone.utc


def allowed(provider) -> bool:
    return bool(getattr(provider, "cache_allowed", False))


def get(db, provider: str, internet_number: str, hd_store_id: str, ttl_minutes: int) -> Optional[Observation]:
    if ttl_minutes <= 0:
        return None
    since = (datetime.now(UTC) - timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
    rows = db._q("SELECT payload FROM provider_cache WHERE provider=? AND internet_number=? AND hd_store_id=? "
                 "AND fetched_at>=?", (provider, internet_number, hd_store_id, since))
    if not rows:
        return None
    try:
        return Observation(**json.loads(rows[0]["payload"]))
    except (TypeError, ValueError):
        return None


def put(db, provider: str, obs: Observation) -> None:
    db._x("INSERT INTO provider_cache(provider, internet_number, hd_store_id, payload, fetched_at) VALUES(?,?,?,?,?) "
          "ON CONFLICT(provider, internet_number, hd_store_id) DO UPDATE SET payload=excluded.payload, "
          "fetched_at=excluded.fetched_at",
          (provider, obs.internet_number, obs.hd_store_id, json.dumps(obs.to_dict()),
           datetime.now(UTC).isoformat(timespec="seconds")))


def clear(db, provider: str | None = None) -> None:
    if provider:
        db._x("DELETE FROM provider_cache WHERE provider=?", (provider,))
    else:
        db._x("DELETE FROM provider_cache")
