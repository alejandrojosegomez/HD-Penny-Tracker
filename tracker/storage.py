"""Storage layer. Two backends behind one small interface:

* SQLiteStorage  – default, zero setup, file in data/tracker.db
* SupabaseStorage – Postgres on Supabase, same tables (supabase/schema.sql)

Both expose the same methods so the rest of the code never cares which one is active.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .settings import settings

UTC = timezone.utc


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  hd_store_id TEXT UNIQUE NOT NULL,
  name TEXT, city TEXT, zip TEXT
);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  internet_number TEXT UNIQUE NOT NULL,
  sku TEXT, upc TEXT, model TEXT,
  name TEXT, description TEXT, brand TEXT,
  category TEXT, image_url TEXT, product_url TEXT,
  source TEXT DEFAULT 'manual',
  active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL REFERENCES products(id),
  store_id INTEGER NOT NULL REFERENCES stores(id),
  price REAL, original_price REAL, discount_pct REAL,
  stock INTEGER, availability TEXT,
  raw TEXT,
  scraped_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ph ON price_history(product_id, store_id, scraped_at DESC);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL REFERENCES products(id),
  store_id INTEGER NOT NULL REFERENCES stores(id),
  score INTEGER NOT NULL,
  reasons TEXT,
  message TEXT,
  sent INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alerts ON alerts(product_id, store_id, created_at DESC);
CREATE TABLE IF NOT EXISTS community_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  internet_number TEXT NOT NULL,
  source TEXT NOT NULL,
  source_url TEXT,
  reported_at TEXT NOT NULL,
  UNIQUE(internet_number, source, reported_at)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  ok INTEGER,
  detail TEXT
);
"""


class SQLiteStorage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # worker and web containers share this file: wait for locks instead of failing,
        # and use WAL so the dashboard can read while the worker writes
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SQLITE_SCHEMA)
        self.conn.commit()

    # ---- helpers -------------------------------------------------------
    def _q(self, sql: str, args: Iterable = ()) -> list[dict]:
        cur = self.conn.execute(sql, tuple(args))
        return [dict(r) for r in cur.fetchall()]

    def _x(self, sql: str, args: Iterable = ()) -> int:
        cur = self.conn.execute(sql, tuple(args))
        self.conn.commit()
        return cur.lastrowid

    # ---- stores --------------------------------------------------------
    def upsert_store(self, hd_store_id: str, name: str, city: str = "", zip: str = "") -> int:
        self._x(
            "INSERT INTO stores(hd_store_id,name,city,zip) VALUES(?,?,?,?) "
            "ON CONFLICT(hd_store_id) DO UPDATE SET name=excluded.name, city=excluded.city, zip=excluded.zip",
            (hd_store_id, name, city, zip),
        )
        return self._q("SELECT id FROM stores WHERE hd_store_id=?", (hd_store_id,))[0]["id"]

    def stores(self) -> list[dict]:
        return self._q("SELECT * FROM stores ORDER BY hd_store_id")

    # ---- products ------------------------------------------------------
    def upsert_product(self, internet_number: str, **fields) -> int:
        cols = {k: v for k, v in fields.items() if v is not None}
        existing = self._q("SELECT id FROM products WHERE internet_number=?", (internet_number,))
        if existing:
            if cols:
                sets = ", ".join(f"{k}=?" for k in cols)
                self._x(f"UPDATE products SET {sets} WHERE internet_number=?", (*cols.values(), internet_number))
            return existing[0]["id"]
        cols["internet_number"] = internet_number
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        return self._x(f"INSERT INTO products({keys}) VALUES({marks})", tuple(cols.values()))

    def products(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM products" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return self._q(sql)

    def product_by_internet_number(self, internet_number: str) -> Optional[dict]:
        rows = self._q("SELECT * FROM products WHERE internet_number=?", (internet_number,))
        return rows[0] if rows else None

    # ---- price history -------------------------------------------------
    def add_price(self, product_id: int, store_id: int, price, original_price, stock, availability, raw: dict | None = None) -> int:
        discount = None
        if price is not None and original_price:
            try:
                discount = round(100.0 * (1 - float(price) / float(original_price)), 2)
            except ZeroDivisionError:
                discount = None
        return self._x(
            "INSERT INTO price_history(product_id,store_id,price,original_price,discount_pct,stock,availability,raw,scraped_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (product_id, store_id, price, original_price, discount, stock, availability,
             json.dumps(raw) if raw else None, now_iso()),
        )

    def history(self, product_id: int, store_id: int, days: int = 120) -> list[dict]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        return self._q(
            "SELECT * FROM price_history WHERE product_id=? AND store_id=? AND scraped_at>=? ORDER BY scraped_at",
            (product_id, store_id, since),
        )

    def latest_prices(self) -> list[dict]:
        """One row per (product, store): the most recent observation, joined with product + store."""
        return self._q(
            """
            SELECT ph.*, p.internet_number, p.sku, p.name, p.brand, p.description, p.category, p.image_url, p.product_url,
                   s.hd_store_id, s.name AS store_name
            FROM price_history ph
            JOIN products p ON p.id = ph.product_id
            JOIN stores s ON s.id = ph.store_id
            WHERE ph.id IN (
              SELECT MAX(id) FROM price_history GROUP BY product_id, store_id
            ) AND p.active = 1
            ORDER BY ph.discount_pct DESC NULLS LAST
            """
        )

    # ---- alerts --------------------------------------------------------
    def last_alert(self, product_id: int, store_id: int) -> Optional[dict]:
        rows = self._q(
            "SELECT * FROM alerts WHERE product_id=? AND store_id=? ORDER BY created_at DESC LIMIT 1",
            (product_id, store_id),
        )
        return rows[0] if rows else None

    def add_alert(self, product_id: int, store_id: int, score: int, reasons: list[str], message: str) -> int:
        return self._x(
            "INSERT INTO alerts(product_id,store_id,score,reasons,message,sent,created_at) VALUES(?,?,?,?,?,0,?)",
            (product_id, store_id, score, json.dumps(reasons), message, now_iso()),
        )

    def mark_alert_sent(self, alert_id: int) -> None:
        self._x("UPDATE alerts SET sent=1 WHERE id=?", (alert_id,))

    def alerts(self, limit: int = 100) -> list[dict]:
        return self._q(
            """
            SELECT a.*, p.internet_number, p.name, p.image_url, p.product_url, s.hd_store_id, s.name AS store_name
            FROM alerts a JOIN products p ON p.id=a.product_id JOIN stores s ON s.id=a.store_id
            ORDER BY a.created_at DESC LIMIT ?
            """,
            (limit,),
        )

    # ---- community -----------------------------------------------------
    def add_community_report(self, internet_number: str, source: str, source_url: str, reported_at: str) -> bool:
        try:
            self._x(
                "INSERT INTO community_reports(internet_number,source,source_url,reported_at) VALUES(?,?,?,?)",
                (internet_number, source, source_url, reported_at),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def recent_reports(self, internet_number: str, days: int = 14) -> list[dict]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        return self._q(
            "SELECT * FROM community_reports WHERE internet_number=? AND reported_at>=? ORDER BY reported_at DESC",
            (internet_number, since),
        )

    # ---- runs ----------------------------------------------------------
    def start_run(self, job: str) -> int:
        return self._x("INSERT INTO runs(job,started_at) VALUES(?,?)", (job, now_iso()))

    def finish_run(self, run_id: int, ok: bool, detail: str = "") -> None:
        self._x("UPDATE runs SET finished_at=?, ok=?, detail=? WHERE id=?", (now_iso(), 1 if ok else 0, detail, run_id))

    def last_runs(self, limit: int = 20) -> list[dict]:
        return self._q("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))


class SupabaseStorage(SQLiteStorage):
    """Supabase backend.

    Keeps a local SQLite mirror for the fast joins the dashboard needs and pushes every write
    to Supabase so the data is also in Postgres (Realtime, Vercel front end, etc.).
    Reads come from the local mirror; the mirror is rebuilt from Supabase on startup if empty.
    """

    def __init__(self, path: Path, url: str, key: str):
        super().__init__(path)
        from supabase import create_client  # imported lazily so sqlite mode has no dependency

        self.sb = create_client(url, key)
        if not self._q("SELECT 1 FROM products LIMIT 1"):
            self._pull_mirror()

    def _pull_mirror(self) -> None:
        for s in self.sb.table("stores").select("*").execute().data or []:
            SQLiteStorage.upsert_store(self, s["hd_store_id"], s.get("name", ""), s.get("city", ""), s.get("zip", ""))
        for p in self.sb.table("products").select("*").execute().data or []:
            p = {k: v for k, v in p.items() if k not in ("id", "created_at")}
            SQLiteStorage.upsert_product(self, p.pop("internet_number"), **p)

    # write-through overrides --------------------------------------------
    def upsert_store(self, hd_store_id, name, city="", zip=""):
        self.sb.table("stores").upsert(
            {"hd_store_id": hd_store_id, "name": name, "city": city, "zip": zip}, on_conflict="hd_store_id"
        ).execute()
        return super().upsert_store(hd_store_id, name, city, zip)

    def upsert_product(self, internet_number, **fields):
        payload = {"internet_number": internet_number, **{k: v for k, v in fields.items() if v is not None}}
        self.sb.table("products").upsert(payload, on_conflict="internet_number").execute()
        return super().upsert_product(internet_number, **fields)

    def _remote_ids(self, product_id: int, store_id: int) -> tuple[Optional[int], Optional[int]]:
        p = self._q("SELECT internet_number FROM products WHERE id=?", (product_id,))
        s = self._q("SELECT hd_store_id FROM stores WHERE id=?", (store_id,))
        if not p or not s:
            return None, None
        rp = self.sb.table("products").select("id").eq("internet_number", p[0]["internet_number"]).limit(1).execute().data
        rs = self.sb.table("stores").select("id").eq("hd_store_id", s[0]["hd_store_id"]).limit(1).execute().data
        return (rp[0]["id"] if rp else None, rs[0]["id"] if rs else None)

    def add_price(self, product_id, store_id, price, original_price, stock, availability, raw=None):
        local_id = super().add_price(product_id, store_id, price, original_price, stock, availability, raw)
        rp, rs = self._remote_ids(product_id, store_id)
        if rp and rs:
            row = self._q("SELECT * FROM price_history WHERE id=?", (local_id,))[0]
            self.sb.table("price_history").insert({
                "product_id": rp, "store_id": rs, "price": row["price"], "original_price": row["original_price"],
                "discount_pct": row["discount_pct"], "stock": row["stock"], "availability": row["availability"],
                "scraped_at": row["scraped_at"],
            }).execute()
        return local_id

    def add_alert(self, product_id, store_id, score, reasons, message):
        local_id = super().add_alert(product_id, store_id, score, reasons, message)
        rp, rs = self._remote_ids(product_id, store_id)
        if rp and rs:
            self.sb.table("alerts").insert({
                "product_id": rp, "store_id": rs, "score": score, "reasons": reasons, "message": message,
            }).execute()
        return local_id

    def add_community_report(self, internet_number, source, source_url, reported_at):
        added = super().add_community_report(internet_number, source, source_url, reported_at)
        if added:
            self.sb.table("community_reports").upsert({
                "internet_number": internet_number, "source": source, "source_url": source_url, "reported_at": reported_at,
            }, on_conflict="internet_number,source,reported_at").execute()
        return added


_storage: Optional[SQLiteStorage] = None


def get_storage() -> SQLiteStorage:
    global _storage
    if _storage is None:
        if settings.database_mode == "supabase" and settings.supabase_url and settings.supabase_key:
            _storage = SupabaseStorage(settings.sqlite_path, settings.supabase_url, settings.supabase_key)
        else:
            _storage = SQLiteStorage(settings.sqlite_path)
        for s in settings.stores:
            _storage.upsert_store(s.store_id, s.name, s.city, s.zip)
    return _storage
