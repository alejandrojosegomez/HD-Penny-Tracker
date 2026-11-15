"""Telegram notifications. Plain Bot API call, no library needed."""
from __future__ import annotations

import logging

import httpx

from .settings import settings

log = logging.getLogger("tracker.notify")


def send(text: str) -> bool:
    if not settings.telegram_token or not settings.telegram_chat_id:
        log.warning("telegram not configured; message was:\n%s", text)
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    try:
        r = httpx.post(url, json={"chat_id": settings.telegram_chat_id, "text": text,
                                  "parse_mode": "HTML", "disable_web_page_preview": False}, timeout=20)
        if r.status_code != 200:
            log.error("telegram %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        log.error("telegram send failed: %s", e)
        return False


def format_alert(product: dict, store: dict, latest: dict, score: int, reasons: list[str]) -> str:
    label = "🔥 GO SCAN TODAY" if score >= settings.alert_threshold else "👀 watch this week"
    name = product.get("name") or f"Item {product['internet_number']}"
    price = latest.get("price")
    orig = latest.get("original_price")
    disc = latest.get("discount_pct")
    price_line = f"${price:.2f}" if price is not None else "n/a"
    if orig and disc is not None:
        price_line += f" (was ${orig:.2f}, {disc:.0f}% off)"
    stock = latest.get("stock")
    avail = latest.get("availability") or "unknown"
    lines = [
        f"<b>{label}</b> — score {score}",
        f"<b>{name}</b>",
        f"Store: {store.get('name')} (#{store.get('hd_store_id')})",
        f"Price: {price_line}",
        f"Stock: {stock if stock is not None else '?'} · {avail.replace('_', ' ')}",
        f"Internet #: <code>{product['internet_number']}</code>" + (f" · SKU <code>{product['sku']}</code>" if product.get("sku") else ""),
        "",
        "Why: " + "; ".join(reasons),
    ]
    if product.get("product_url"):
        lines.append(product["product_url"])
    return "\n".join(lines)


def test_message() -> bool:
    return send("HD Penny Tracker is connected ✅")
