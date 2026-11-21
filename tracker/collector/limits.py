"""Per-provider request budget, kept in the database so it holds across restarts and
across the worker and web containers.

* per minute: when the window is full, wait until a slot frees up (never burst past it)
* per day (UTC): when used up, refuse; the job stops and resumes on the next schedule
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

UTC = timezone.utc
log = logging.getLogger("tracker.collector")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="microseconds")


class RequestBudget:
    def __init__(self, db, provider: str, per_minute: int = 0, per_day: int = 0,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.db, self.provider = db, provider
        self.per_minute, self.per_day = per_minute, per_day
        self.clock, self.sleep = clock, sleep
        # keep the table small: nothing older than two days is ever counted
        db._x("DELETE FROM provider_requests WHERE at < ?", (_iso(clock() - 2 * 86400),))

    def used_today(self) -> int:
        now = datetime.fromtimestamp(self.clock(), UTC)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db._q("SELECT COUNT(*) AS c FROM provider_requests WHERE provider=? AND at>=?",
                          (self.provider, start.isoformat(timespec="microseconds")))[0]["c"]

    def acquire(self) -> tuple[bool, str]:
        """Reserve one request. Returns (allowed, reason when not allowed)."""
        if self.per_day and self.used_today() >= self.per_day:
            return False, f"daily request limit reached ({self.per_day}/day for {self.provider})"
        if self.per_minute:
            now = self.clock()
            rows = self.db._q("SELECT at FROM provider_requests WHERE provider=? AND at>=? ORDER BY at",
                              (self.provider, _iso(now - 60)))
            if len(rows) >= self.per_minute:
                oldest = datetime.fromisoformat(rows[len(rows) - self.per_minute]["at"]).timestamp()
                wait = oldest + 60 - now
                if wait > 0:
                    log.info("%s: %d requests in the last minute (limit %d); waiting %.1fs",
                             self.provider, len(rows), self.per_minute, wait)
                    self.sleep(wait)
        self.db._x("INSERT INTO provider_requests(provider, at) VALUES(?, ?)", (self.provider, _iso(self.clock())))
        return True, ""


def usage(db, day: str | None = None) -> list[dict]:
    """Requests per provider for one UTC day (default today)."""
    day = day or datetime.now(UTC).date().isoformat()
    nxt = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    return db._q("SELECT provider, COUNT(*) AS requests FROM provider_requests WHERE at>=? AND at<? "
                 "GROUP BY provider ORDER BY provider", (day, nxt))
