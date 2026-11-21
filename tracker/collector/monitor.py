"""One snapshot of how collection is going, for the dashboard and GET /v1/monitoring."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config, jobs, limits

UTC = timezone.utc


def price_changes(db, since: str, limit: int = 20) -> tuple[int, list[dict]]:
    """Rows whose price differs from the previous row for the same product and store,
    whatever the source (collector, API, manual entry)."""
    sql = """
      WITH h AS (
        SELECT ph.id, ph.product_id, ph.store_id, ph.price, ph.scraped_at, ph.raw,
               LAG(ph.price) OVER (PARTITION BY ph.product_id, ph.store_id ORDER BY ph.id) AS prev_price,
               LAG(ph.id) OVER (PARTITION BY ph.product_id, ph.store_id ORDER BY ph.id) AS prev_id
        FROM price_history ph
      )
      SELECT h.id, h.price, h.prev_price, h.scraped_at, json_extract(h.raw, '$.source') AS source,
             p.internet_number, p.name, s.hd_store_id, s.name AS store_name
      FROM h JOIN products p ON p.id = h.product_id JOIN stores s ON s.id = h.store_id
      WHERE h.prev_id IS NOT NULL AND h.price IS NOT h.prev_price AND h.scraped_at >= ?
      ORDER BY h.id DESC
    """
    rows = db._q(sql, (since,))
    return len(rows), rows[:limit]


def snapshot(db) -> dict:
    now = datetime.now(UTC)
    day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    today = now.date().isoformat()

    recent = jobs.recent(db, 20)
    running = [j for j in recent if j["status"] in ("queued", "running")]
    last_ok = jobs.recent(db, 1, status="succeeded")
    last_ok = last_ok[0] if last_ok else None
    last = recent[0] if recent else None
    day_jobs = [j for j in recent if (j.get("created_at") or "") >= day_ago]

    err_rows = jobs.errors(db, 200, since=day_ago)
    by_status: dict[str, int] = {}
    for e in err_rows:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1

    n_changes, changes = price_changes(db, day_ago)
    return {
        "time": now.isoformat(timespec="seconds"),
        "collection_status": "running" if running else (last["status"] if last else "never_run"),
        "running_jobs": running,
        "last_job": last,
        "last_successful_collection": (last_ok or {}).get("finished_at"),
        "last_successful_job": last_ok,
        "last_job_products_processed": (last or {}).get("products_processed", 0),
        "last_job_stores_processed": (last or {}).get("stores_processed", 0),
        "last_24h": {
            "jobs": len(day_jobs),
            "products_processed": sum(j.get("products_processed") or 0 for j in day_jobs),
            "stores_processed": max([j.get("stores_processed") or 0 for j in day_jobs] or [0]),
            "saved": sum(j.get("saved") or 0 for j in day_jobs),
            "rejected": sum(j.get("rejected") or 0 for j in day_jobs),
            "price_changes": n_changes,
            "provider_errors": len(err_rows),
            "provider_errors_by_status": by_status,
        },
        "provider_usage_today": [
            {**u, "limit_per_day": config.max_per_day() or None} for u in limits.usage(db)],
        "api_usage_today": db._q("SELECT key_name, requests, last_path, last_at FROM api_usage WHERE day=? "
                                 "ORDER BY requests DESC", (today,)),
        "api_usage_7d": db._q("SELECT key_name, SUM(requests) AS requests FROM api_usage WHERE day>=? "
                              "GROUP BY key_name ORDER BY requests DESC",
                              ((now - timedelta(days=6)).date().isoformat(),)),
        "recent_errors": jobs.errors(db, 20),
        "recent_price_changes": changes,
        "recent_jobs": recent[:10],
    }
