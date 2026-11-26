"""API tests with FastAPI's TestClient against a throwaway SQLite db."""
import importlib
import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_MODE", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SCRAPE_ENABLED", "true")
    # never reach the real Telegram bot from tests, even with a filled-in .env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    # fresh settings/storage for this test; reload everything that imported them, so
    # detect/manual write to the throwaway db and not data/tracker.db
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


def test_manual_price_scores_and_alerts(client, monkeypatch):
    import tracker.detect as d
    sent = []
    monkeypatch.setattr(d, "send", lambda msg: sent.append(msg) or True)
    # new product, deep clearance with stock: saved, scored, alert sent
    r = client.post("/api/prices", json={"internet_number": "335104182", "store": "0260", "price": 0.01,
                                         "original_price": 59.97, "qty": 3, "name": "Work Light"})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["discount_pct"] == pytest.approx(99.98, abs=0.01) and j["availability"] == "in_stock"
    assert j["score"] >= 50 and j["hd_store_id"] == "0260"
    items = client.get("/api/items").json()
    assert len(items) == 1 and items[0]["price"] == 0.01 and "Anotar" not in items[0]["name"]
    # the original price is remembered for the next entry; qty 0 means out of stock
    r = client.post("/api/prices", json={"internet_number": "335104182", "store": "6381", "price": 5.0, "qty": 0})
    assert r.status_code == 201 and r.json()["original_price"] == 59.97 and r.json()["availability"] == "out_of_stock"
    prods = client.get("/api/products").json()
    assert prods[0]["internet_number"] == "335104182" and prods[0]["last_original_price"] == 59.97


def test_manual_price_validation(client):
    bad = [
        {"internet_number": "123", "store": "0260", "price": 1, "original_price": 2},
        {"internet_number": "335104182", "store": "9999", "price": 1, "original_price": 2},
        {"internet_number": "335104182", "store": "0260", "price": 10, "original_price": 5},
        {"internet_number": "335104182", "store": "0260", "price": 1},           # no original price known yet
    ]
    for b in bad:
        r = client.post("/api/prices", json=b)
        assert r.status_code == 400, (b, r.text)
        assert isinstance(r.json()["detail"], str)
    assert client.post("/api/prices", json={"internet_number": "335104182", "store": "0260", "price": 0}).status_code == 422
    assert client.get("/api/items").json() == []
    assert client.get("/api/products").json() == []     # rejected input creates nothing


def test_scrape_job_refused_when_disabled(client, monkeypatch):
    import tracker.web as w
    monkeypatch.setattr(w.settings, "scrape_enabled", False)
    assert client.post("/api/jobs/scrape").status_code == 409
    assert client.get("/api/health").json()["scrape_enabled"] is False
