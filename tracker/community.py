"""Pulls items from the public community penny lists and decides which ones to watch.

Uses the same structured parsers as the brain (brain/community.py), so a product is only
watched when the list links to its Home Depot page and we get a real 9-digit internet
number. SKUs, UPCs, phone numbers and zip codes are never treated as internet numbers.

The community watch set is kept small on purpose (COMMUNITY_MAX, default 60): every item
costs one Home Depot request per store per scrape, and too many requests get the IP blocked.
Ranking: reported in Florida first, then most reports, then most recent. Community items
that drop out of the set are deactivated (history kept). Manual items are never touched.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta

import httpx

from brain.community import PARSERS, SOURCES, UA

from .settings import settings
from .storage import get_storage

log = logging.getLogger("tracker.community")

INET_RE = re.compile(r"^\d{9}$")
RECENT_DAYS = 14


def fetch_items(sources: dict = SOURCES) -> tuple[list[dict], dict]:
    """Downloads and parses every source. Returns (items, per-source stats)."""
    today = date.today().isoformat()
    items, stats = [], {}
    for name, url in sources.items():
        try:
            r = httpx.get(url, headers=UA, timeout=40, follow_redirects=True)
            r.raise_for_status()
            parsed = PARSERS[name](r.text, today)
            for it in parsed:
                it["source"], it["source_url"] = name, url
            items += parsed
            stats[name] = {"found": len(parsed),
                           "with_internet_number": sum(1 for i in parsed if _inet(i))}
        except Exception as e:  # one broken source must not stop the others
            log.warning("%s failed: %s", name, e)
            stats[name] = f"error: {e}"
        time.sleep(1)
    return items, stats


def _inet(item: dict) -> str | None:
    n = (item.get("internet_number") or "").strip()
    return n if INET_RE.match(n) else None


def pick_watch_set(items: list[dict], limit: int, today: date | None = None) -> list[dict]:
    """One entry per internet number, recent only, best candidates first."""
    today = today or date.today()
    cutoff = (today - timedelta(days=RECENT_DAYS)).isoformat()
    best: dict[str, dict] = {}
    for it in items:
        n = _inet(it)
        if not n or (it.get("reported_at") or "") < cutoff:
            continue
        states = {s for s in (it.get("states") or "").split(",") if s}
        cur = best.setdefault(n, {"internet_number": n, "name": None, "fl": False, "reports": 0,
                                  "last": "", "sources": set()})
        cur["name"] = cur["name"] or it.get("name")
        cur["fl"] |= "FL" in states
        cur["reports"] = max(cur["reports"], it.get("report_count") or 0)
        cur["last"] = max(cur["last"], it.get("reported_at") or "")
        cur["sources"].add(it["source"])
    ranked = sorted(best.values(), key=lambda c: (c["fl"], c["reports"], c["last"]), reverse=True)
    return ranked[:limit]


def refresh(sources: dict = SOURCES) -> dict:
    db = get_storage()
    run_id = db.start_run("community")
    items, stats = fetch_items(sources)
    try:
        # reports keyed by internet number, so detect.py's "on a penny list" signal matches
        new_reports = 0
        for it in items:
            n = _inet(it)
            if n and db.add_community_report(n, it["source"], it["source_url"], it.get("reported_at")):
                new_reports += 1

        if not any(isinstance(v, dict) for v in stats.values()):
            raise RuntimeError("every community source failed; watch set left unchanged")

        watch = pick_watch_set(items, settings.community_max)
        keep = {w["internet_number"] for w in watch}
        added = 0
        for w in watch:
            existing = db.product_by_internet_number(w["internet_number"])
            if existing and existing.get("source") != "community":
                continue                         # manual item: leave it as the user set it
            if not existing or not existing.get("active"):
                added += 1
            db.upsert_product(w["internet_number"], name=(existing or {}).get("name") or w["name"],
                              source="community", active=1)
        retired = 0
        for p in db.products(active_only=True):
            if p.get("source") == "community" and p["internet_number"] not in keep:
                db.upsert_product(p["internet_number"], active=0)
                retired += 1

        summary = {"sources": stats, "new_reports": new_reports, "watching": len(watch),
                   "added": added, "retired": retired}
        db.finish_run(run_id, True, str(summary))
        log.info("community refresh: %s", summary)
        return summary
    except Exception as e:
        db.finish_run(run_id, False, f"{e} | {stats}")
        raise


def ran_today() -> bool:
    """True if a successful community refresh already started today (UTC date)."""
    from .storage import now_iso
    today = now_iso()[:10]
    for r in get_storage().last_runs(50):
        if r["job"] == "community" and r.get("ok") and (r.get("started_at") or "")[:10] == today:
            return True
    return False
