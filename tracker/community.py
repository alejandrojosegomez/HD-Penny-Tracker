"""Pulls Home Depot internet numbers from public community penny lists.

These are small static sites; they are fetched once a day with a plain client and a
descriptive user agent. Every 9-digit number found on the page is treated as a candidate
internet number, de-duplicated, stored in community_reports and added to the watchlist
with source=community so the next scrape checks it in my stores.

Add or remove sources in SOURCES. Each entry is (name, url).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from .settings import append_watchlist
from .storage import get_storage

log = logging.getLogger("tracker.community")

SOURCES = [
    ("pennycentral", "https://www.pennycentral.com/"),
    ("pennyrecon", "https://www.pennyrecon.com/"),
    ("pennypinchinmom", "https://pennypinchinmom.com/home-depot-penny-list/"),
    ("paydirt", "https://paydirt.gg/home-depot-penny-list/"),
]

UA = "HD-Penny-Tracker/1.0 (personal price tracker; contact via GitHub alejandrojosegomez)"
# Internet numbers are 9 digits (older, e.g. 203401615) or 10 digits (newer, e.g. 1008631746).
# Also accepts the dashed 1008-631-746 style some lists use.
NUM_RE = re.compile(r"(?<!\d)(\d{3,4})-?(\d{3})-?(\d{3})(?!\d)")


def _extract_numbers(text: str) -> set[str]:
    out = set()
    for a, b, c in NUM_RE.findall(text):
        n = a + b + c
        # internet numbers start with 1,2 or 3 in practice; drop obvious phone numbers / zips glued together
        if n[0] in "123" and len(n) in (9, 10):
            out.add(n)
    return out


def fetch_source(name: str, url: str) -> set[str]:
    r = httpx.get(url, headers={"user-agent": UA}, timeout=40, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    nums = _extract_numbers(text)
    log.info("%s: %d candidate numbers", name, len(nums))
    return nums


def refresh() -> dict:
    db = get_storage()
    run_id = db.start_run("community")
    today = datetime.now(timezone.utc).date().isoformat()
    stats = {}
    for name, url in SOURCES:
        try:
            nums = fetch_source(name, url)
        except Exception as e:
            log.warning("%s failed: %s", name, e)
            stats[name] = f"error: {e}"
            continue
        new_reports = new_watch = 0
        for n in nums:
            if db.add_community_report(n, name, url, today):
                new_reports += 1
            if append_watchlist(n, category="", note=f"from {name}", source="community"):
                new_watch += 1
        stats[name] = {"found": len(nums), "new_reports": new_reports, "added_to_watchlist": new_watch}
    db.finish_run(run_id, True, str(stats))
    log.info("community refresh: %s", stats)
    return stats
