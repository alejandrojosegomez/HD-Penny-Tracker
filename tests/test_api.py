"""API tests with FastAPI's TestClient against a throwaway SQLite db."""
import os
import importlib
import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_MODE", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    # fresh settings/storage for this test
    import tracker.settings as s
    importlib.reload(s)
    import tracker.storage as st
    importlib.reload(st)
    import tracker.web as w
    importlib.reload(w)
    # isolate the watchlist csv too
    monkeypatch.setattr(w, "append_watchlist", lambda n, c="", note="", src="manual": True)
    from fastapi.testclient import TestClient
    return TestClient(w.app)


def test_health_and_stores(client):
    r = client.get("/api/health"); assert r.status_code == 200
    body = r.json(); assert body["status"] == "ok" and body["stores"] == 4
    r = client.get("/api/stores"); assert r.status_code == 200 and len(r.json()) == 4


def test_add_validate_remove(client):
    assert client.post("/api/items/123").status_code == 400
    r = client.post("/api/items/203401615?category=patio"); assert r.status_code == 201 and r.json()["added"]
    assert client.get("/api/history/203401615").status_code == 200
    assert client.get("/api/history/999999999").status_code == 404
    r = client.delete("/api/items/203401615"); assert r.status_code == 200
    assert client.get("/api/items").json() == []


def test_items_filters_and_schema(client):
    import tracker.web as w
    db = w.get_storage()
    pid = db.upsert_product("203401615", name="Patio Set", brand="Hampton Bay", category="patio")
    s = db.stores()[0]
    db.add_price(pid, s["id"], 9.03, 100.0, 2, "in_stock")
    items = client.get("/api/items").json()
    assert len(items) == 1 and items[0]["discount_pct"] == pytest.approx(90.97) and items[0]["score"] >= 30
    assert client.get("/api/items?store=0000").json() == []
    assert client.get("/api/items?min_score=100").json() == []
    assert len(client.get("/api/items?q=hampton").json()) == 1
    assert client.get("/api/items?min_score=101").status_code == 422


def test_jobs_and_docs(client):
    r = client.post("/api/jobs/detect"); assert r.status_code == 202 and r.json()["status"] == "started"
    assert client.post("/api/jobs/bogus").status_code == 422
    spec = client.get("/openapi.json").json()
    assert {"Item", "Alert", "Store", "Health"} <= set(spec["components"]["schemas"])
