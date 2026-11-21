"""Checks every observation must pass before it is saved (on top of Observation's own checks).

Returns a list of problems; an empty list means the observation is valid. Used by the
collector and by POST /v1/observations, so both apply the same rules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from ..providers.base import Observation
from . import config

UTC = timezone.utc


def parse_time(value: str) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # observed_at without a zone is local time (Observation's default); compare as local
    return t if t.tzinfo else t.astimezone()


def validate(obs: Observation, known_stores: Iterable[str], now: Optional[datetime] = None) -> list[str]:
    problems: list[str] = []
    now = now or datetime.now(UTC)

    if len(obs.internet_number) not in (9, 10):
        problems.append("internet_number must be 9 or 10 digits")
    if obs.hd_store_id not in set(known_stores):
        problems.append(f"store {obs.hd_store_id} is not in config/stores.yaml")
    if obs.sku is not None and not str(obs.sku).isdigit():
        problems.append("sku must be digits")
    if obs.price is not None and obs.price > config.max_price():
        problems.append(f"price {obs.price} is above the {config.max_price():.0f} limit")
    if obs.original_price is not None and obs.original_price > config.max_price():
        problems.append(f"original_price {obs.original_price} is above the {config.max_price():.0f} limit")
    if obs.price is not None and obs.original_price is not None and obs.original_price < obs.price:
        problems.append("original_price is lower than price")
    if obs.stock is not None and obs.stock < 0:
        problems.append("stock cannot be negative")

    t = parse_time(obs.observed_at)
    if t is None:
        problems.append(f"observed_at is not an ISO time: {obs.observed_at!r}")
    else:
        if t > now + timedelta(minutes=5):
            problems.append("observed_at is in the future")
        if t < now - timedelta(days=config.max_age_days()):
            problems.append(f"observed_at is older than {config.max_age_days()} days")
    return problems
