"""Scoring. Turns a product's price history in one store into a 0-100 "go scan it" score.

Signals (see README for the table):
  depth        – how deep the discount is right now
  markdowns    – how many distinct markdowns we have seen
  dwell        – how long it has sat at the current deep price with stock
  zma          – price snapped back to full and the item went unavailable (remote penny hint)
  community    – reported on a public penny list recently, and my store shows stock
  inactive     – flagged discontinued / unavailable while still answering by id
  seasonal     – seasonal category (bonus, small)
  ending       – .02/.03 price ending (tiny, on purpose)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .notify import format_alert, send
from .settings import settings
from .storage import get_storage

log = logging.getLogger("tracker.detect")

DEEP = 90.0            # % off considered "deep"
FULL_PRICE_TOL = 0.02  # within 2% of original counts as "back to full price"


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def score_history(history: list[dict], category: str | None, reports: list[dict]) -> tuple[int, list[str]]:
    """history is oldest→newest for one (product, store)."""
    if not history:
        return 0, []
    now = datetime.now(timezone.utc)
    latest = history[-1]
    price = latest.get("price")
    orig = latest.get("original_price") or price
    disc = latest.get("discount_pct") or 0.0
    avail = latest.get("availability") or "unknown"
    stock = latest.get("stock")
    score, reasons = 0, []

    # --- depth --------------------------------------------------------
    if disc >= 99:
        score += 50; reasons.append(f"{disc:.0f}% off")
    elif disc >= 95:
        score += 40; reasons.append(f"{disc:.0f}% off")
    elif disc >= DEEP:
        score += 30; reasons.append(f"{disc:.0f}% off")
    elif disc >= 75:
        score += 15; reasons.append(f"{disc:.0f}% off")

    # --- markdown count (distinct decreasing prices) ---------------------
    prices = []
    for h in history:
        p = h.get("price")
        if p is None:
            continue
        if not prices or abs(p - prices[-1]) > 0.009:
            prices.append(p)
    drops = sum(1 for a, b in zip(prices, prices[1:]) if b < a)
    if drops >= 2:
        score += 25; reasons.append(f"{drops + 1} markdowns seen")
    elif drops == 1:
        score += 15; reasons.append("2nd markdown seen")

    # --- dwell at current price with stock -------------------------------
    if price is not None and disc >= 75:
        since = latest["scraped_at"]
        for h in reversed(history):
            if h.get("price") is not None and abs(h["price"] - price) <= 0.009:
                since = h["scraped_at"]
            else:
                break
        days = (now - _dt(since)).days
        has_stock = avail in ("in_stock", "limited") or (stock or 0) > 0
        if has_stock and days > 30:
            score += 25; reasons.append(f"{days}d at this price with stock")
        elif has_stock and days > 14:
            score += 15; reasons.append(f"{days}d at this price with stock")

    # --- ZMA flip: deep clearance -> full price + gone ---------------------
    if price is not None and orig and avail in ("unavailable", "out_of_stock") and abs(price - orig) <= orig * FULL_PRICE_TOL:
        recent = [h for h in history[:-1] if (now - _dt(h["scraped_at"])).days <= 7]
        had_deep_stock = any((h.get("discount_pct") or 0) >= 75 and
                             (h.get("availability") in ("in_stock", "limited") or (h.get("stock") or 0) > 0)
                             for h in recent)
        if had_deep_stock:
            score += 40; reasons.append("ZMA flip: back to full price + unavailable after deep clearance")

    # --- community report ---------------------------------------------------
    if reports:
        srcs = sorted({r["source"] for r in reports})
        if avail in ("in_stock", "limited") or (stock or 0) > 0:
            score += 35; reasons.append(f"on penny list ({', '.join(srcs)}) and my store shows stock")
        else:
            score += 15; reasons.append(f"on penny list ({', '.join(srcs)})")

    # --- inactive / discontinued -------------------------------------------
    raw = latest.get("raw") or ""
    if "discontinued\": true" in raw or '"discontinued": true' in raw:
        score += 20; reasons.append("marked discontinued")

    # --- seasonal -------------------------------------------------------------
    if category and category.lower() in settings.seasonal_categories:
        score += 10; reasons.append(f"seasonal ({category})")

    # --- price ending (weak) ---------------------------------------------------
    if price is not None:
        cents = round(price * 100) % 100
        if cents in (2, 3):
            score += 5; reasons.append(f"price ends .{cents:02d}")

    return min(score, 100), reasons


def run(send_alerts: bool = True) -> dict:
    db = get_storage()
    run_id = db.start_run("detect")
    created = sent = 0
    products = {p["id"]: p for p in db.products()}
    stores = db.stores()
    for p in products.values():
        reports = db.recent_reports(p["internet_number"])
        for s in stores:
            hist = db.history(p["id"], s["id"])
            if not hist:
                continue
            score, reasons = score_history(hist, p.get("category"), reports)
            if score < settings.watch_threshold:
                continue
            last = db.last_alert(p["id"], s["id"])
            # don't repeat the same alert; re-alert only if the score went up by 10+ or 3 days passed
            if last and last["score"] >= score - 9 and (datetime.now(timezone.utc) - _dt(last["created_at"])).days < 3:
                continue
            latest = hist[-1]
            msg = format_alert(p, s, latest, score, reasons)
            alert_id = db.add_alert(p["id"], s["id"], score, reasons, msg)
            created += 1
            if send_alerts and score >= settings.alert_threshold and send(msg):
                db.mark_alert_sent(alert_id)
                sent += 1
    summary = f"detect done: {created} alerts created, {sent} sent"
    db.finish_run(run_id, True, summary)
    log.info(summary)
    return {"created": created, "sent": sent}
