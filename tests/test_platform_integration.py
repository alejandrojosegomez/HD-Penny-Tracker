"""Collector + persistence + API together, and compatibility with manual entry, Telegram
alerts and the Penny Brain."""
import json
import subprocess
import sys
from pathlib import Path

from tracker.collector import run_collection
from tracker.providers.mock import MockProvider

A, B = "1006423739", "1004871058"
ROOT = Path(__file__).resolve().parents[1]


def count(db, sql="SELECT COUNT(*) c FROM price_history"):
    return db._q(sql)[0]["c"]


def test_collector_writes_and_api_reads_the_same_rows(platform):
    c, h = platform.client, platform.brain
    res = run_collection(internet_numbers=[A, B], provider=MockProvider.from_file())
    assert res["saved"] == 5
    d = c.get(f"/v1/products/{A}", headers=h).json()
    assert {x["hd_store_id"]: x["price"] for x in d["latest"]} == {"0260": 0.06, "6381": 129.0, "6336": 64.5}
    assert all(x["source"] == "mock" and x["mocked"] for x in d["latest"])
    assert c.get("/v1/stores/0260/prices", headers=h).json()[0]["internet_number"] in (A, B)
    # the existing dashboard API sees the same data
    items = c.get("/api/items").json()
    assert len(items) == 5 and {i["internet_number"] for i in items} == {A, B}


def test_repeated_runs_create_no_duplicate_rows(platform):
    db = platform.db
    p = MockProvider.from_file()
    p.cache_allowed = False                  # ask the provider every time; same answers come back
    for _ in range(3):
        run_collection(internet_numbers=[A, B], provider=p)
    assert count(db) == 5
    assert count(db, "SELECT COUNT(*) c FROM products") == 2
    assert db._q("SELECT SUM(duplicates) d FROM collection_jobs")[0]["d"] == 10


def test_api_job_and_cli_style_job_share_one_write_path(platform):
    c, h = platform.client, platform.brain
    c.post("/v1/collect", json={"internet_numbers": [A], "stores": ["0260"]}, headers=h)
    run_collection(internet_numbers=[A], stores=["0260"], provider=MockProvider.from_file())
    c.post("/v1/observations", json={"internet_number": A, "hd_store_id": "0260", "price": 0.06,
                                     "original_price": 129.0, "stock": 2, "availability": "limited"},
           headers=platform.bot)
    sources = [json.loads(r["raw"])["source"] for r in platform.db._q("SELECT raw FROM price_history ORDER BY id")]
    # mock answer saved once (second run deduped/cached), API submission is a different source
    assert sources == ["mock", "api:telegram-bot"]


# ---------------------------------------------------------------- manual entry
def test_manual_entry_still_writes_exactly_one_row_and_api_sees_it(platform):
    m, db = platform.manual, platform.db
    m.record_price(A, "0260", 0.06, original_price=129.0, qty=2, send_alerts=False)
    m.record_price(A, "0260", 0.03, send_alerts=False)
    assert count(db) == 2
    hist = platform.client.get(f"/v1/prices/{A}/history?store=0260", headers=platform.brain).json()
    assert [(r["price"], r["source"]) for r in hist["stores"]["0260"]] == [(0.06, "manual"), (0.03, "manual")]


def test_manual_and_collected_prices_live_in_one_history(platform):
    m = platform.manual
    m.record_price(A, "0260", 50.0, original_price=129.0, send_alerts=False)
    run_collection(internet_numbers=[A], stores=["0260"], provider=MockProvider.from_file())
    hist = platform.db.history(platform.db.product_by_internet_number(A)["id"],
                               platform.db.stores()[0]["id"])
    assert [h["price"] for h in hist] == [50.0, 0.06]
    m2 = platform.client.get("/api/monitoring").json()
    assert m2["last_24h"]["price_changes"] == 1


def test_dashboard_manual_price_endpoint_unchanged(platform):
    r = platform.client.post("/api/prices", json={"internet_number": A, "store": "0260", "price": 0.06,
                                                  "original_price": 129.0, "qty": 1})
    assert r.status_code == 201 and r.json()["score"] >= 0
    assert count(platform.db) == 1


# ---------------------------------------------------------------- telegram
def _ago(h):
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(hours=h)).isoformat(timespec="seconds")


MARKDOWNS = [(64.5, 3), (10.0, 2), (0.01, 1)]      # price, hours ago: 2 markdowns then 99% off = score 75


def test_single_penny_price_scores_below_alert_threshold(platform):
    """Existing scoring rule, unchanged: one 99%-off sighting = 50 points, no Telegram message."""
    platform.client.post("/v1/observations", json={"internet_number": A, "hd_store_id": "0260", "price": 0.01,
                                                   "original_price": 129.0, "stock": 3}, headers=platform.bot)
    assert platform.sent == []
    assert platform.db._q("SELECT score FROM alerts")[0]["score"] == 50      # recorded as "watch"


def test_telegram_alert_fires_for_collected_markdowns(platform):
    p = MockProvider([{"internet_number": A, "hd_store_id": "0260", "sequence": [
        {"price": pr, "original_price": 129.0, "stock": 3, "availability": "in_stock", "observed_at": _ago(h)}
        for pr, h in MARKDOWNS]}])
    p.cache_allowed = False
    for _ in MARKDOWNS:
        run_collection(internet_numbers=[A], stores=["0260"], provider=p)
    platform.detect.run()
    assert len(platform.sent) == 1 and "GO SCAN TODAY" in platform.sent[0] and "0.01" in platform.sent[0]


def test_telegram_alert_fires_for_api_submitted_markdowns(platform):
    for pr, h in MARKDOWNS:
        r = platform.client.post("/v1/observations", json={
            "internet_number": A, "hd_store_id": "0260", "price": pr, "original_price": 129.0, "stock": 3,
            "availability": "in_stock", "observed_at": _ago(h)}, headers=platform.bot)
        assert r.status_code == 201
    # detect runs after each API submission, like manual entry
    assert len(platform.sent) == 1 and "GO SCAN TODAY" in platform.sent[0]
    assert platform.db._q("SELECT MAX(score) s FROM alerts")[0]["s"] >= 70


def test_rescore_can_be_skipped(platform):
    platform.client.post("/v1/observations?rescore=false",
                         json={"internet_number": A, "hd_store_id": "0260", "price": 0.01,
                               "original_price": 129.0, "stock": 3}, headers=platform.bot)
    assert platform.sent == []


def test_telegram_notice_when_the_scheduled_scrape_is_refused(platform):
    platform.db.upsert_product(A, source="test")
    p = MockProvider([{"internet_number": A, "hd_store_id": "0260", "status": "rate_limited", "detail": "HTTP 429"}])
    res = platform.scraper.scrape_all(provider=p, trigger="schedule")
    assert res["stopped"] and len(platform.sent) == 1 and "rate_limited" in platform.sent[0]


# ---------------------------------------------------------------- penny brain
def test_penny_brain_is_untouched_and_its_tests_pass():
    """The brain keeps its own CSV state and never imports the tracker; run its suite as is."""
    brain_src = " ".join(p.read_text() for p in (ROOT / "brain").glob("*.py"))
    assert "from tracker" not in brain_src and "import tracker" not in brain_src
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-W", "ignore::UserWarning",
                        "tests/test_brain.py", "tests/test_brain_e2e.py", "tests/test_brain_alert.py",
                        "tests/test_brain_insights.py", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


def test_penny_brain_can_consume_the_api_with_its_own_key(platform):
    """The brain (or anything else) can read store prices with its key and push what it
    learns as observations, attributed to it."""
    c = platform.client
    run_collection(internet_numbers=[A], provider=MockProvider.from_file())
    prices = c.get("/v1/stores/0260/prices", headers=platform.brain).json()
    assert prices and prices[0]["price_scope"] == "store"
    r = c.post("/v1/observations", json={"internet_number": B, "hd_store_id": "6336", "price": 12.0,
                                         "original_price": 99.97}, headers=platform.brain)
    assert r.json()["source"] == "api:penny-brain"
