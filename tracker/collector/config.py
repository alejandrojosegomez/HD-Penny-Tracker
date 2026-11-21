"""Collector settings, read from the environment each time (so tests and .env edits apply)."""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


def max_per_minute() -> int:
    """Requests per minute per provider, on top of any lower limit the provider declares. 0 = none."""
    return _int("COLLECTOR_MAX_PER_MINUTE", 30)


def max_per_day() -> int:
    """Requests per UTC day per provider. When reached, jobs stop until the next day. 0 = none."""
    return _int("COLLECTOR_MAX_PER_DAY", 2000)


def cache_ttl_minutes() -> int:
    """How long a cached answer is reused, for providers that allow caching. 0 = no cache."""
    return _int("COLLECTOR_CACHE_TTL_MINUTES", 60)


def max_price() -> float:
    """Prices above this are rejected as implausible."""
    return _float("COLLECTOR_MAX_PRICE", 50000.0)


def max_age_days() -> int:
    """Observations older than this are rejected."""
    return _int("COLLECTOR_MAX_AGE_DAYS", 30)


def effective_limit(env_limit: int, provider_limit: int) -> int:
    """The stricter of two limits, where 0 means 'no limit'."""
    vals = [v for v in (env_limit, provider_limit) if v and v > 0]
    return min(vals) if vals else 0
