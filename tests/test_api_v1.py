"""The authenticated /v1 API on its own: auth, every endpoint, observation submission rules."""
import json
from datetime import datetime, timedelta

import pytest

A, B = "1006423739", "1004871058"


def obs(**kw):
    return {"internet_number": A, "hd_store_id": "0260", "price": 0.06, "original_price": 129.0,
            "stock": 2, "availability": "limited", **kw}


# ---------------------------------------------------------------- auth
def test_health_needs_no_key(platform):
    r = platform.client.get("/v1/health")
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok" and b["database"] == "ok" and b["api_keys_configured"] is True
    assert b["price_provider"] == "mock"


@pytest.mark.parametrize("path", ["/v1/products", "/v1/stores", "/v1/jobs", "/v1/monitoring",
                                  f"/v1/prices/{A}/history", "/v1/stores/0260/prices"])
def test_every_other_route_needs_a_valid_key(platform, path):
    c = platform.client
    assert c.get(path).status_code == 401
    assert c.get(path, headers={"X-API-Key": "wrong-key-0123456789"}).status_code == 401
    assert c.get(path, headers=platform.brain).status_code in (200, 404)


def test_posts_need_a_key(platform):
    assert platform.client.post("/v1/observations", json=obs()).status_code == 401
    assert platform.client.post("/v1/collect", json={}).status_code == 401


def test_no_keys_configured_means_api_closed(platform, monkeypatch):
    monkeypatch.setenv("API_KEYS", "")
    r = platform.client.get("/v1/stores", headers=platform.brain)
    assert r.status_code == 503 and "API_KEYS" in r.json()["detail"]


def test_short_keys_are_ignored(platform, monkeypatch):
    monkeypatch.setenv("API_KEYS", "weak:short")
    assert platform.client.get("/v1/stores", headers={"X-API-Key": "short"}).status_code == 503


def test_usage_is_counted_once_per_request_per_key(platform):
    c = platform.client
    for _ in range(3):
        c.get("/v1/stores", headers=platform.brain)
    c.post("/v1/observations", json=obs(), headers=platform.bot)
    rows = {r["key_name"]: r["requests"] for r in platform.db._q("SELECT * FROM api_usage")}
    assert rows == {"penny-brain": 3, "telegram-bot": 1}


# ---------------------------------------------------------------- stores
def test_stores(platform):
    c, h = platform.client, platform.brain
    r = c.get("/v1/stores", headers=h)
    assert r.status_code == 200 and {s["hd_store_id"] for s in r.json()} == {"0260", "6381", "6336", "0430"}
    assert c.get("/v1/stores/260", headers=h).json()["name"] == "West Melbourne"
    assert c.get("/v1/stores/9999", headers=h).status_code == 404


# ---------------------------------------------------------------- observations
def test_submit_observation_creates_one_row_with_attribution(platform):
    c = platform.client
    r = c.post("/v1/observations", json=obs(sku="1001234567", name="Vanity light", note="shelf tag"),
               headers=platform.bot)
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["created"] and b["source"] == "api:telegram-bot" and not b["price_changed"]
    rows = platform.db._q("SELECT raw FROM price_history")
    assert len(rows) == 1
    raw = json.loads(rows[0]["raw"])
    assert raw["source"] == "api:telegram-bot" and raw["submitted_by"] == "telegram-bot" and raw["note"] == "shelf tag"
    p = platform.db.product_by_internet_number(A)
    assert p["sku"] == "1001234567" and p["name"] == "Vanity light"


def test_resubmitting_the_same_observation_is_not_a_duplicate_row(platform):
    c = platform.client
    t = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    r1 = c.post("/v1/observations", json=obs(observed_at=t), headers=platform.bot)
    r2 = c.post("/v1/observations", json=obs(observed_at=t), headers=platform.bot)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.json()["created"] is False and r2.json()["id"] == r1.json()["id"]
    assert platform.db._q("SELECT COUNT(*) c FROM price_history")[0]["c"] == 1


def test_price_change_reported(platform):
    c = platform.client
    c.post("/v1/observations", json=obs(price=10.0), headers=platform.bot)
    r = c.post("/v1/observations", json=obs(price=5.0), headers=platform.bot)
    assert r.json()["price_changed"] and r.json()["previous_price"] == 10.0


@pytest.mark.parametrize("bad, why", [
    ({"price": 10.0, "original_price": 5.0}, "lower than price"),
    ({"hd_store_id": "9999"}, "not in config"),
    ({"internet_number": "12345"}, "9 or 10 digits"),
    ({"observed_at": "yesterday"}, "ISO"),
    ({"observed_at": "2099-01-01T00:00:00"}, "future"),
])
def test_invalid_observations_rejected_and_logged(platform, bad, why):
    r = platform.client.post("/v1/observations", json=obs(**bad), headers=platform.bot)
    assert r.status_code == 422 and why in json.dumps(r.json())
    assert platform.db._q("SELECT COUNT(*) c FROM price_history")[0]["c"] == 0
    assert platform.db._q("SELECT COUNT(*) c FROM provider_errors WHERE status='rejected'")[0]["c"] == 1


def test_schema_errors_are_422(platform):
    r = platform.client.post("/v1/observations", json=obs(price=-1), headers=platform.bot)
    assert r.status_code == 422
    r = platform.client.post("/v1/observations", json=obs(availability="nope"), headers=platform.bot)
    assert r.status_code == 422


# ---------------------------------------------------------------- products + history
def test_products_detail_sku_and_history(platform):
    c, h = platform.client, platform.brain
    c.post("/v1/observations", json=obs(price=20.0, sku="1001234567", name="Light"), headers=platform.bot)
    c.post("/v1/observations", json=obs(price=10.0), headers=platform.bot)
    c.post("/v1/observations", json=obs(hd_store_id="6381", price=129.0), headers=platform.bot)

    lst = c.get("/v1/products", headers=h).json()
    assert [p["internet_number"] for p in lst] == [A]
    assert c.get("/v1/products?q=light", headers=h).json()[0]["name"] == "Light"
    assert c.get("/v1/products?q=zzz", headers=h).json() == []

    d = c.get(f"/v1/products/{A}", headers=h).json()
    latest = {x["hd_store_id"]: x for x in d["latest"]}
    assert latest["0260"]["price"] == 10.0 and latest["6381"]["price"] == 129.0
    assert latest["0260"]["source"] == "api:telegram-bot" and latest["0260"]["mocked"] is False

    assert c.get("/v1/products/by-sku/1001234567", headers=h).json()["internet_number"] == A
    assert c.get("/v1/products/by-sku/42", headers=h).status_code == 404
    assert c.get("/v1/products/by-sku/abc", headers=h).status_code == 400
    assert c.get(f"/v1/products/{B}", headers=h).status_code == 404
    assert c.get("/v1/products/12", headers=h).status_code == 400

    hist = c.get(f"/v1/prices/{A}/history", headers=h).json()
    assert [r["price"] for r in hist["stores"]["0260"]] == [20.0, 10.0] and hist["stores"]["0430"] == []
    one = c.get(f"/v1/prices/{A}/history?store=6381&days=7", headers=h).json()
    assert list(one["stores"]) == ["6381"] and one["days"] == 7

    sp = c.get("/v1/stores/0260/prices", headers=h).json()
    assert len(sp) == 1 and sp[0]["internet_number"] == A and sp[0]["price"] == 10.0


def test_inactive_products_hidden_unless_asked(platform):
    platform.db.upsert_product(A, active=0)
    c, h = platform.client, platform.brain
    assert c.get("/v1/products", headers=h).json() == []
    assert len(c.get("/v1/products?include_inactive=true", headers=h).json()) == 1


# ---------------------------------------------------------------- jobs
def test_collect_job_runs_and_reports_status(platform):
    c, h = platform.client, platform.brain
    r = c.post("/v1/collect", json={"internet_numbers": [A], "stores": ["0260", "6381"]}, headers=h)
    assert r.status_code == 202 and r.json()["provider"] == "mock"
    job_id = r.json()["job_id"]
    # TestClient runs background tasks before returning, so the job is finished here
    j = c.get(f"/v1/jobs/{job_id}", headers=h).json()
    assert j["status"] == "succeeded" and j["trigger"] == "api" and j["saved"] == 2
    assert j["products_processed"] == 1 and j["stores_processed"] == 2
    assert j["params"]["internet_numbers"] == [A]
    assert c.get("/v1/jobs", headers=h).json()[0]["id"] == job_id
    assert c.get("/v1/jobs?status=failed", headers=h).json() == []
    assert c.get("/v1/jobs/999", headers=h).status_code == 404


def test_collect_rejects_bad_requests(platform):
    c, h = platform.client, platform.brain
    assert c.post("/v1/collect", json={"provider": "nope"}, headers=h).status_code == 400
    assert c.post("/v1/collect", json={"stores": ["9999"]}, headers=h).status_code == 400
    assert c.post("/v1/collect", json={"internet_numbers": ["1"]}, headers=h).status_code == 400
    # SCRAPE_ENABLED=false keeps the blocked direct client off
    assert c.post("/v1/collect", json={"provider": "hd_direct"}, headers=h).status_code == 409


def test_collect_by_sku(platform):
    c, h = platform.client, platform.brain
    j = c.post("/v1/collect", json={"skus": ["1009876543"], "stores": ["0260"]}, headers=h).json()
    assert c.get(f"/v1/jobs/{j['job_id']}", headers=h).json()["saved"] == 1
    assert c.get("/v1/products/by-sku/1009876543", headers=h).json()["internet_number"] == B


# ---------------------------------------------------------------- monitoring
def test_monitoring(platform):
    c, h = platform.client, platform.brain
    m = c.get("/v1/monitoring", headers=h).json()
    assert m["collection_status"] == "never_run" and m["last_successful_collection"] is None
    c.post("/v1/collect", json={"internet_numbers": [A, B]}, headers=h)
    c.post("/v1/observations", json=obs(price=0.03), headers=platform.bot)      # change from mock's 0.06
    m = c.get("/v1/monitoring", headers=h).json()
    assert m["collection_status"] == "succeeded" and m["last_successful_collection"]
    assert m["last_job_products_processed"] == 2 and m["last_job_stores_processed"] == 4
    assert m["last_24h"]["price_changes"] == 1
    assert m["recent_price_changes"][0]["price"] == 0.03 and m["recent_price_changes"][0]["prev_price"] == 0.06
    assert {u["provider"]: u["requests"] for u in m["provider_usage_today"]} == {"mock": 8}
    assert {u["key_name"] for u in m["api_usage_today"]} == {"penny-brain", "telegram-bot"}
    # the dashboard's local copy is the same data, no key
    assert c.get("/api/monitoring").json()["last_24h"]["price_changes"] == 1


def test_openapi_documents_the_key(platform):
    spec = platform.client.get("/openapi.json").json()
    assert "APIKeyHeader" in spec["components"]["securitySchemes"]
    assert "/v1/observations" in spec["paths"] and "/v1/health" in spec["paths"]
