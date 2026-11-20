"""Regression guard: one manual price submission writes exactly one price_history row.

record_price() saves through providers.save.save_observation(). If a future change also
writes the row another way (for example a leftover db.add_price() call), each submission
would create two rows and skew the price history and alert scoring. These tests catch that.
"""
import importlib
import json

import pytest

ITEM = "1006423739"
STORE = "0260"


@pytest.fixture()
def manual(tmp_path, monkeypatch):
    """record_price() wired to a throwaway SQLite db; nothing touches data/ or Telegram."""
    monkeypatch.setenv("DATABASE_MODE", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "manual.db"))
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(k, "")
    import tracker.settings as s
    importlib.reload(s)
    import tracker.storage as st
    importlib.reload(st)
    import tracker.notify as nt
    importlib.reload(nt)
    import tracker.detect as d
    importlib.reload(d)
    import tracker.manual as m
    importlib.reload(m)
    monkeypatch.setattr(d, "send", lambda msg: True)

    db = st.get_storage()
    db_file = db.conn.execute("PRAGMA database_list").fetchone()[2]
    assert db_file == str(tmp_path / "manual.db")      # never the real data/tracker.db
    return m, db


def _rows(db) -> list[dict]:
    return db._q(
        "SELECT h.price, h.original_price, h.stock, h.raw FROM price_history h "
        "JOIN products p ON p.id = h.product_id JOIN stores s ON s.id = h.store_id "
        "WHERE p.internet_number = ? AND s.hd_store_id = ? ORDER BY h.id", (ITEM, STORE))


def _total(db) -> int:
    return db._q("SELECT COUNT(*) AS c FROM price_history")[0]["c"]


def test_first_submission_creates_exactly_one_row(manual):
    m, db = manual
    assert _total(db) == 0

    m.record_price(ITEM, STORE, 0.03, original_price=129.0, qty=1, send_alerts=False)

    assert _total(db) == 1
    rows = _rows(db)
    assert len(rows) == 1
    assert (rows[0]["price"], rows[0]["original_price"], rows[0]["stock"]) == (0.03, 129.0, 1)
    assert json.loads(rows[0]["raw"])["source"] == "manual"


def test_each_later_submission_adds_exactly_one_row(manual):
    m, db = manual
    m.record_price(ITEM, STORE, 0.06, original_price=129.0, qty=2, send_alerts=False)
    assert _total(db) == 1

    # same product and store; original price reused from the first entry
    m.record_price(ITEM, STORE, 0.03, qty=1, send_alerts=False)
    assert _total(db) == 2

    m.record_price(ITEM, STORE, 0.01, send_alerts=False)
    assert _total(db) == 3

    assert [r["price"] for r in _rows(db)] == [0.06, 0.03, 0.01]
    assert all(json.loads(r["raw"])["source"] == "manual" for r in _rows(db))
