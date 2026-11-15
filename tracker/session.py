"""Bootstraps a real browser session once and hands the cookies to curl_cffi.

Home Depot runs Akamai Bot Manager. A real Chromium passes the sensor check and gets the
_abck / bm_sz / ak_bmsc cookies; those are reused for the fast requests. When the server
starts refusing, we throw the session away and bootstrap again.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Optional

from .settings import settings

log = logging.getLogger("tracker.session")

BOOT_URL = "https://www.homedepot.com/"
# a stable, boring product page used only to warm the session and select a store
WARM_URL = "https://www.homedepot.com/p/202521104"


@dataclass
class BrowserSession:
    cookies: dict
    user_agent: str
    created_at: float

    @property
    def age_hours(self) -> float:
        return (time.time() - self.created_at) / 3600

    def save(self) -> None:
        settings.session_file.write_text(json.dumps(asdict(self)))

    @classmethod
    def load(cls) -> Optional["BrowserSession"]:
        if not settings.session_file.exists():
            return None
        try:
            return cls(**json.loads(settings.session_file.read_text()))
        except Exception:
            return None


def bootstrap(store_id: Optional[str] = None, zip_code: Optional[str] = None) -> BrowserSession:
    """Open Chromium, load Home Depot, optionally localize to a store, export cookies."""
    from playwright.sync_api import sync_playwright

    log.info("bootstrapping browser session (headless=%s)", settings.headless)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.headless, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 850},
            locale="en-US",
            timezone_id=settings.timezone,
        )
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(BOOT_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2500)
        page.goto(WARM_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2500)

        if store_id:
            # Home Depot localizes by cookie; setting it directly is the most reliable way.
            ctx.add_cookies([{
                "name": "THD_LOCALIZER",
                "value": _localizer_cookie(store_id, zip_code or ""),
                "domain": ".homedepot.com",
                "path": "/",
            }])
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1500)

        ua = page.evaluate("navigator.userAgent")
        cookies = {c["name"]: c["value"] for c in ctx.cookies() if "homedepot" in c.get("domain", "")}
        browser.close()

    sess = BrowserSession(cookies=cookies, user_agent=ua, created_at=time.time())
    sess.save()
    log.info("session ready: %d cookies", len(cookies))
    return sess


def _localizer_cookie(store_id: str, zip_code: str) -> str:
    # URL-encoded JSON, same shape the site writes when you pick a store.
    import urllib.parse

    payload = {"WORKFLOW": "LOCALIZED_BY_STORE", "THD_FORCE_LOC": "1", "THD_INTERNAL": "0",
               "THD_LOCSTORE": store_id, "THD_STRFINDERZIP": zip_code, "THD_STORE_HOURS": "", "THD_STORE_HOURS_EXPIRY": "0"}
    return urllib.parse.quote(json.dumps(payload, separators=(",", ":")))


def get_session(force: bool = False, max_age_hours: float = 6.0) -> BrowserSession:
    sess = None if force else BrowserSession.load()
    if sess is None or sess.age_hours > max_age_hours:
        first = settings.stores[0] if settings.stores else None
        sess = bootstrap(first.store_id if first else None, first.zip if first else None)
    return sess
