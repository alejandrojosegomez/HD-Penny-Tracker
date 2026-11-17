"""Telegram alerts. Without TELEGRAM_TOKEN / TELEGRAM_CHAT_ID it just prints (dry run)."""
from __future__ import annotations

import os

import requests


def send(text: str) -> bool:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[telegram dry-run] {text}")
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=15,
    )
    if not r.ok:
        print(f"[telegram error {r.status_code}] {r.text[:200]}")
    return r.ok
