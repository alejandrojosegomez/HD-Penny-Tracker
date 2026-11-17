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


# ------------------------------------------------- animated status message ---
ASSETS = __import__("pathlib").Path(__file__).parent / "assets"


def _api(method: str, **kw):
    token, chat_id = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    files = kw.pop("files", None)
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          data={"chat_id": chat_id, **kw}, files=files, timeout=30)
        return r.json().get("result") if r.ok else None
    except requests.RequestException:
        return None


class Progress:
    """A 'Procesando datos' animation that updates its caption at each step and is
    replaced by a 'done' animation at the end. Never raises: status is cosmetic."""

    def __init__(self, enabled: bool = True):
        self.enabled, self.msg_id, self.steps = enabled, None, []

    def start(self, title: str):
        if not self.enabled:
            return
        with open(ASSETS / "processing.gif", "rb") as f:
            res = _api("sendAnimation", caption=f"⏳ {title}", files={"animation": f})
        self.msg_id = res and res.get("message_id")
        self.title = title

    def step(self, text: str):
        print(f"[progress] {text}")
        self.steps.append(text)
        if self.msg_id:
            lines = [f"✅ {s}" for s in self.steps[:-1]] + [f"🔄 {self.steps[-1]}…"]
            _api("editMessageCaption", message_id=self.msg_id,
                 caption=f"⏳ {self.title}\n\n" + "\n".join(lines))

    def finish(self, caption: str, ok: bool = True):
        if not self.enabled:
            return
        if self.msg_id:
            _api("deleteMessage", message_id=self.msg_id)
        if ok:
            with open(ASSETS / "done.gif", "rb") as f:
                _api("sendAnimation", caption=caption, files={"animation": f})
        else:
            _api("sendMessage", text=caption)
