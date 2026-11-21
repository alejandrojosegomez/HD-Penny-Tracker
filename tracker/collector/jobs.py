"""collection_jobs and provider_errors: what ran, what it did, what went wrong."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

UTC = timezone.utc
COUNTERS = ("products_processed", "stores_processed", "requests", "cached", "saved", "duplicates",
            "rejected", "unavailable", "errors", "price_changes")
STATUSES = ("queued", "running", "succeeded", "stopped", "failed", "skipped")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create(db, trigger: str, provider: str, params: Optional[dict] = None, status: str = "queued") -> int:
    return db._x("INSERT INTO collection_jobs(trigger, provider, status, params, created_at) VALUES(?,?,?,?,?)",
                 (trigger, provider, status, json.dumps(params or {}), _now()))


def start(db, job_id: int) -> None:
    db._x("UPDATE collection_jobs SET status='running', started_at=? WHERE id=?", (_now(), job_id))


def finish(db, job_id: int, status: str, counts: dict, detail: str = "") -> None:
    assert status in STATUSES
    sets = ", ".join(f"{c}=?" for c in COUNTERS)
    db._x(f"UPDATE collection_jobs SET status=?, finished_at=?, detail=?, {sets} WHERE id=?",
          (status, _now(), detail, *(int(counts.get(c, 0)) for c in COUNTERS), job_id))


def get(db, job_id: int) -> Optional[dict]:
    rows = db._q("SELECT * FROM collection_jobs WHERE id=?", (job_id,))
    return _decode(rows[0]) if rows else None


def recent(db, limit: int = 20, status: Optional[str] = None) -> list[dict]:
    if status:
        rows = db._q("SELECT * FROM collection_jobs WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
    else:
        rows = db._q("SELECT * FROM collection_jobs ORDER BY id DESC LIMIT ?", (limit,))
    return [_decode(r) for r in rows]


def _decode(row: dict) -> dict:
    try:
        row["params"] = json.loads(row.get("params") or "{}")
    except ValueError:
        row["params"] = {}
    return row


def record_error(db, job_id: Optional[int], provider: str, status: str, internet_number: str = "",
                 hd_store_id: str = "", detail: str = "") -> None:
    db._x("INSERT INTO provider_errors(job_id, provider, status, internet_number, hd_store_id, detail, created_at) "
          "VALUES(?,?,?,?,?,?,?)", (job_id, provider, status, internet_number, hd_store_id, detail[:500], _now()))


def errors(db, limit: int = 50, since: Optional[str] = None) -> list[dict]:
    if since:
        return db._q("SELECT * FROM provider_errors WHERE created_at>=? ORDER BY id DESC LIMIT ?", (since, limit))
    return db._q("SELECT * FROM provider_errors ORDER BY id DESC LIMIT ?", (limit,))
