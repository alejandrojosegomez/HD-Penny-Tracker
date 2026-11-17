"""Community penny feed -> local SQLite database (brain/state/brain.db).

    python -m brain.community          # fetch all sources, save, print summary
    python -m brain.community --top 30 # show the 30 hottest items

Sources (plain HTML, robots.txt allows these pages; fetched once a day):
  pennyrecon  - cards with SKU, internet #, retail, last seen, #reports, #states
  paydirt     - weekly list as JSON-LD (name + SKU/UPC), dated
  ppm         - Penny Pinchin' Mom list, "Name - SKU: 1003-951-328"
PennyCentral and Hidden Clearances render with JavaScript; add their items with
add_reports() (e.g. from a browser read) using source='pennycentral'/'hiddenclearances'.
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "state" / "brain.db"
UA = {"User-Agent": "Mozilla/5.0 (personal penny tracker; 1 request/day per source)"}

SOURCES = {
    "pennyrecon": "https://www.pennyrecon.com/",
    # Paydirt's page loads this JSON itself; it holds the last ~4 weekly lists.
    "paydirt": "https://paydirt.gg/penny/hd.json",
    "ppm": "https://pennypinchinmom.com/home-depot-penny-list/",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS community_reports (
  id INTEGER PRIMARY KEY,
  sku TEXT NOT NULL,                 -- digits only, e.g. '1015277793' or '215583'
  internet_number TEXT,
  upc TEXT,
  name TEXT,
  retail REAL,
  source TEXT NOT NULL,
  source_url TEXT,
  reported_at TEXT NOT NULL,         -- date the source says it was seen (YYYY-MM-DD)
  report_count INTEGER,              -- how many people reported it (if the source says)
  states TEXT,                       -- comma list, e.g. 'FL,TX'
  receipt_confirmed INTEGER DEFAULT 0,
  first_collected TEXT NOT NULL,
  last_collected TEXT NOT NULL,
  UNIQUE (sku, source, reported_at)
);
CREATE INDEX IF NOT EXISTS cr_sku ON community_reports (sku);
CREATE INDEX IF NOT EXISTS cr_inet ON community_reports (internet_number);
CREATE TABLE IF NOT EXISTS collect_runs (
  id INTEGER PRIMARY KEY, source TEXT, ran_at TEXT, status TEXT, items INTEGER, error TEXT
);
"""


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# ---------------------------------------------------------------- parsers ---
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _parse_mdy(text: str) -> str | None:
    m = re.search(r"\b(" + "|".join(MONTHS) + r")[a-z]* (\d{1,2}), (\d{4})", text)
    if m:
        return date(int(m[3]), MONTHS.index(m[1][:3]) + 1, int(m[2])).isoformat()
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if m:
        return date(int(m[3]), int(m[1]), int(m[2])).isoformat()
    return None


def parse_pennyrecon(page: str, today: str) -> list[dict]:
    out = []
    for card in re.findall(r'<article class="card[^"]*".*?</article>', page, re.S):
        sku = digits(re.search(r'data-copy="([\d-]+)"', card)[1]) if 'data-copy="' in card else ""
        if not sku:
            continue
        name = re.search(r"<h3>.*?>([^<]+)</a></h3>", card, re.S)
        inet = re.search(r'homedepot\.com/p/(?:[^"]*/)?(\d{9})"', card)
        retail = re.search(r"\$([\d,.]+) retail", card)
        seen = re.search(r'<span title="([^"]+)">Last seen', card)
        reports = re.search(r"(\d+) reports?<", card)
        states = re.search(r'data-states="([^"]*)"', card)
        out.append({
            "sku": sku,
            "internet_number": inet[1] if inet else None,
            "name": htmllib.unescape(name[1]).strip() if name else None,
            "retail": float(retail[1].replace(",", "")) if retail else None,
            "reported_at": (_parse_mdy(seen[1]) if seen else None) or today,
            "report_count": int(reports[1]) if reports else None,
            "states": states[1] if states else None,
            "receipt_confirmed": int("Receipt confirmed" in card),
        })
    return out


def parse_paydirt(page: str, today: str) -> list[dict]:
    """Paydirt hd.json: {"weeks":[{"weekKey":"2026-11-09","sources":[..],"items":[...]}]}.
    Falls back to the JSON-LD on the HTML page if given HTML."""
    try:
        data = json.loads(page)
    except json.JSONDecodeError:
        return _parse_paydirt_html(page, today)
    out = []
    for wk in data.get("weeks", []):
        upstream = ",".join(wk.get("sources", []))
        for it in wk.get("items", []):
            ident = digits(it.get("sku")) or ""
            upc = digits(it.get("upc")) or None
            if not ident and not upc:
                continue
            inet = re.search(r"/(\d{9})(?:$|[/?])", it.get("url", ""))
            out.append({
                "sku": ident or f"upc:{upc}", "upc": upc,
                "internet_number": inet[1] if inet else None,
                "name": it.get("name"), "reported_at": wk.get("weekKey") or today,
                "report_count": it.get("weeksOnList"),   # weeks on list, best proxy they publish
                "states": None, "upstream": upstream,
            })
    return out


def _parse_paydirt_html(page: str, today: str) -> list[dict]:
    out = []
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "ItemList" or "penny" not in data.get("name", "").lower():
            continue
        when = _parse_mdy(data.get("name", "")) or today
        for el in data.get("itemListElement", []):
            name = el.get("name", "")
            m = re.search(r"\((SKU|UPC) ([\d-]+)\)\s*$", name)
            if not m:
                continue
            ident = digits(m[2])
            out.append({
                "sku": ident if m[1] == "SKU" else f"upc:{ident}",
                "upc": ident if m[1] == "UPC" else None,
                "name": name[: m.start()].strip(),
                "reported_at": when,
            })
    return out


def parse_ppm(page: str, today: str) -> list[dict]:
    text = htmllib.unescape(re.sub(r"<[^>]+>", "\n", page))
    when = None
    for d in re.findall(r"Home Depot (?:Deals|Penny List) for (\d{1,2}/\d{1,2}/\d{4})", text):
        when = max(when or "", _parse_mdy(d) or "")
    out = []
    for m in re.finditer(r"([^\n]{5,200}?)\s*[–-]?\s*SKU:?\s*(\d{4}-\d{3}-\d{3}|\d{6})", text):
        out.append({"sku": digits(m[2]), "name": m[1].strip(" –-"), "reported_at": when or today})
    return out


PARSERS = {"pennyrecon": parse_pennyrecon, "paydirt": parse_paydirt, "ppm": parse_ppm}


# ------------------------------------------------------------------ store ---
def add_reports(con: sqlite3.Connection, source: str, items: list[dict],
                source_url: str | None = None) -> int:
    """Upsert reports; returns rows inserted or updated."""
    now = datetime.now().isoformat(timespec="seconds")
    new = 0
    for it in items:
        cur = con.execute(
            """INSERT INTO community_reports
               (sku, internet_number, upc, name, retail, source, source_url, reported_at,
                report_count, states, receipt_confirmed, first_collected, last_collected)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (sku, source, reported_at) DO UPDATE SET
                 report_count = COALESCE(excluded.report_count, report_count),
                 states = COALESCE(excluded.states, states),
                 internet_number = COALESCE(excluded.internet_number, internet_number),
                 receipt_confirmed = MAX(receipt_confirmed, excluded.receipt_confirmed),
                 last_collected = excluded.last_collected""",
            (it["sku"], it.get("internet_number"), it.get("upc"), it.get("name"),
             it.get("retail"), source, source_url, it["reported_at"], it.get("report_count"),
             it.get("states"), it.get("receipt_confirmed", 0), now, now),
        )
        new += cur.rowcount
    con.commit()
    return new


def collect(con: sqlite3.Connection | None = None, sources=SOURCES) -> dict[str, int]:
    con = con or connect()
    today = date.today().isoformat()
    before = con.execute("SELECT COUNT(*) FROM community_reports").fetchone()[0]
    counts = {}
    for name, url in sources.items():
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            items = PARSERS[name](r.text, today)
            if not items:
                raise ValueError("0 items parsed - page layout may have changed")
            add_reports(con, name, items, url)
            counts[name] = len(items)
            con.execute("INSERT INTO collect_runs (source, ran_at, status, items) VALUES (?,?,?,?)",
                        (name, datetime.now().isoformat(timespec="seconds"), "ok", len(items)))
        except Exception as e:  # fail loudly but keep going with other sources
            counts[name] = -1
            print(f"[community] {name} FAILED: {e}")
            con.execute("INSERT INTO collect_runs (source, ran_at, status, items, error) VALUES (?,?,?,?,?)",
                        (name, datetime.now().isoformat(timespec="seconds"), "error", 0, str(e)[:500]))
        con.commit()
        time.sleep(2)
    counts["_new_rows"] = con.execute("SELECT COUNT(*) FROM community_reports").fetchone()[0] - before
    return counts


def is_reported(con: sqlite3.Connection, internet_number: str | None = None,
                sku: str | None = None, days: int = 14) -> bool:
    q = ["date(reported_at) >= date('now', ?)"]
    args: list = [f"-{days} day"]
    keys = []
    if internet_number:
        keys.append("internet_number = ?"); args.append(internet_number)
    if sku:
        keys.append("sku = ?"); args.append(digits(sku))
    if not keys:
        return False
    sql = f"SELECT 1 FROM community_reports WHERE {q[0]} AND ({' OR '.join(keys)}) LIMIT 1"
    return con.execute(sql, args).fetchone() is not None


def hottest(con: sqlite3.Connection, days: int = 14, limit: int = 25) -> list[sqlite3.Row]:
    """Items seen recently, ranked by #sources, reports and receipt confirmation."""
    return con.execute(
        """SELECT sku, MAX(name) AS name, MAX(internet_number) AS internet_number,
                  MAX(retail) AS retail, COUNT(DISTINCT source) AS sources,
                  COALESCE(MAX(report_count), 0) AS reports, MAX(receipt_confirmed) AS receipt,
                  MAX(reported_at) AS last_seen, GROUP_CONCAT(DISTINCT source) AS seen_on,
                  MAX(states) AS states
           FROM community_reports
           WHERE date(reported_at) >= date('now', ?)
           GROUP BY sku
           ORDER BY sources DESC, receipt DESC, reports DESC, last_seen DESC
           LIMIT ?""", (f"-{days} day", limit)).fetchall()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    con = connect()
    if not a.no_fetch:
        print("collected:", collect(con))
    total = con.execute("SELECT COUNT(*), COUNT(DISTINCT sku) FROM community_reports").fetchone()
    print(f"database: {total[0]} reports, {total[1]} distinct items\n")
    for r in hottest(con, limit=a.top):
        fl = " FL!" if r["states"] and "FL" in r["states"].split(",") else ""
        print(f"{r['sources']} src | {r['reports']:>3} rep | {'receipt' if r['receipt'] else '       '} | "
              f"{r['last_seen']} | SKU {r['sku']:<10} | ${r['retail'] or 0:>7.2f} | {(r['name'] or '')[:60]}{fl}")
