"""Community watch set: only real internet numbers, ranked, capped, retired when gone."""
import importlib
from datetime import date, timedelta

import pytest

TODAY = date.today()


def _item(n, src="pennyrecon", states="", reports=1, days_ago=0, sku="1015277793"):
    return {"internet_number": n, "sku": sku, "name": f"item {n}", "states": states,
            "report_count": reports, "reported_at": (TODAY - timedelta(days=days_ago)).isoformat(),
            "source": src, "source_url": "u"}


@pytest.fixture()
def comm(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("COMMUNITY_MAX", "3")
    import tracker.settings as s; importlib.reload(s)
    import tracker.storage as st; importlib.reload(st)
    import tracker.community as c; importlib.reload(c)
    return c


def test_only_real_internet_numbers(comm):
    items = [_item("318430226"), _item(None), _item("3215551234"),   # phone number
             _item("1015277793"), _item("12345")]                     # SKU, junk
    picked = comm.pick_watch_set(items, 10)
    assert [p["internet_number"] for p in picked] == ["318430226"]


def test_ranking_cap_and_recency(comm):
    items = [_item("100000001", reports=50), _item("100000002", states="TX,FL", reports=2),
             _item("100000003", reports=10), _item("100000004", reports=99, days_ago=30),  # too old
             _item("100000005", reports=5)]
    picked = [p["internet_number"] for p in comm.pick_watch_set(items, 3)]
    assert picked == ["100000002", "100000001", "100000003"]   # Florida first, then reports


def test_refresh_adds_retires_and_keeps_manual(comm, monkeypatch):
    db = comm.get_storage()
    db.upsert_product("200000000", source="manual")                      # manual: never touched
    monkeypatch.setattr(comm, "fetch_items", lambda s=None: (
        [_item("100000001"), _item("100000002"), _item("200000000")], {"pennyrecon": {"found": 3}}))
    r = comm.refresh()
    assert r["watching"] == 3 and r["added"] == 2 and r["retired"] == 0
    assert db.product_by_internet_number("200000000")["source"] == "manual"
    assert len(db.recent_reports("100000001")) == 1                      # matches detect's lookup

    monkeypatch.setattr(comm, "fetch_items", lambda s=None: (
        [_item("100000002")], {"pennyrecon": {"found": 1}}))
    r = comm.refresh()
    assert r["retired"] == 1
    active = {p["internet_number"] for p in db.products()}
    assert active == {"100000002", "200000000"}
    assert comm.ran_today()


def test_all_sources_down_keeps_watch_set(comm, monkeypatch):
    db = comm.get_storage()
    monkeypatch.setattr(comm, "fetch_items", lambda s=None: ([_item("100000001")], {"pennyrecon": {"found": 1}}))
    comm.refresh()
    monkeypatch.setattr(comm, "fetch_items", lambda s=None: ([], {"pennyrecon": "error: down"}))
    with pytest.raises(RuntimeError):
        comm.refresh()
    assert {p["internet_number"] for p in db.products()} == {"100000001"}
    assert not db.last_runs(1)[0]["ok"]
