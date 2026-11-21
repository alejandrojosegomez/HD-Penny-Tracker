"""Collection engine on its own: targets, SKUs, budget, cache, validation, refusals, job records."""
import json
from datetime import datetime, timedelta

import pytest

from tracker.collector import jobs, run_collection
from tracker.collector.cache import get as cache_get
from tracker.collector.limits import RequestBudget
from tracker.collector.validate import validate
from tracker.providers import get_provider
from tracker.providers.base import Observation, capabilities_of
from tracker.providers.mock import MockProvider

A, B = "1006423739", "1004871058"


def ph_count(db):
    return db._q("SELECT COUNT(*) AS c FROM price_history")[0]["c"]


def test_registry_and_capabilities(platform):
    p = get_provider("mock")
    assert p.name == "mock" and p.mocked
    assert capabilities_of(p)["sku_lookup"] is True
    from tracker.providers.licensed_api import LicensedApiProvider
    # nothing is assumed for a real source until declared after verification
    assert not any(capabilities_of(LicensedApiProvider(api_key="", base_url="")).values())
    assert LicensedApiProvider(api_key="", base_url="").cache_allowed is False


def test_full_run_saves_validated_observations_with_attribution(platform):
    db = platform.db
    for n in (A, B):
        db.upsert_product(n, source="test")
    res = run_collection(provider=get_provider("mock"), trigger="manual")
    # A: 0260, 6381, 6336 ok, 0430 unavailable; B: 0260, 6381 ok, 6336 + 0430 unavailable
    assert (res["saved"], res["unavailable"], res["stopped"]) == (5, 3, False)
    assert (res["products_processed"], res["stores_processed"]) == (2, 4)
    raw = json.loads(db._q("SELECT raw FROM price_history ORDER BY id LIMIT 1")[0]["raw"])
    assert raw["source"] == "mock" and raw["mocked"] is True and raw["job_id"] == res["job_id"]
    assert raw["price_scope"] == "store" and raw["provider"] == "mock"
    job = jobs.get(db, res["job_id"])
    assert job["status"] == "succeeded" and job["saved"] == 5 and job["trigger"] == "manual"
    assert db.last_runs(1)[0]["job"] == "collect" and db.last_runs(1)[0]["ok"] == 1


def test_single_product_single_store(platform):
    p = MockProvider.from_file()
    res = run_collection(internet_numbers=[A], stores=["0260"], provider=p)
    assert p.requests == [(A, "0260")]
    assert res["saved"] == 1 and res["products_processed"] == 1 and res["stores_processed"] == 1


def test_sku_lookup_known_and_via_provider(platform):
    db = platform.db
    p = MockProvider.from_file()
    res = run_collection(skus=["1001234567"], stores=["0260"], provider=p)
    assert res["saved"] == 1 and db.product_by_sku("1001234567")["internet_number"] == A
    # second time it's known locally: no lookup request, one fetch
    before = res["requests"]
    res2 = run_collection(skus=["1001234567"], stores=["6381"], provider=p)
    assert res2["requests"] == 1 and before == 2


def test_unknown_sku_and_bad_targets(platform):
    res = run_collection(skus=["5555555555"], provider=MockProvider.from_file())
    assert res["saved"] == 0 and res["unavailable"] == 1
    assert any("unknown SKU" in e["detail"] for e in jobs.errors(platform.db))
    bad = run_collection(stores=["9999"], provider=MockProvider.from_file())
    assert "unknown store" in bad["error"] and jobs.get(platform.db, bad["job_id"])["status"] == "failed"
    bad = run_collection(internet_numbers=["12ab"], provider=MockProvider.from_file())
    assert "9 or 10 digits" in bad["error"]


def test_refusal_stops_job_and_notifies_once(platform):
    # stores run in id order: 0260, 0430, 6336, 6381
    p = MockProvider([
        {"internet_number": A, "hd_store_id": "0260", "price": 1.0, "original_price": 2.0},
        {"internet_number": A, "hd_store_id": "0430", "status": "access_denied", "detail": "HTTP 403"},
        {"internet_number": A, "hd_store_id": "6336", "price": 1.0, "original_price": 2.0},
    ])
    sent = []
    res = run_collection(internet_numbers=[A, B], provider=p, notify=sent.append)
    assert res["stopped"] and res["saved"] == 1 and res["blocked"] == 1
    assert p.requests == [(A, "0260"), (A, "0430")]           # nothing after the refusal
    assert len(sent) == 1 and "access_denied" in sent[0]
    assert jobs.get(platform.db, res["job_id"])["status"] == "stopped"
    assert jobs.errors(platform.db)[0]["status"] == "access_denied"


def test_validation_rejects_bad_observations(platform):
    now = datetime.now().isoformat(timespec="seconds")
    future = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
    p = MockProvider([
        {"internet_number": A, "hd_store_id": "0260", "price": 10.0, "original_price": 5.0},        # orig < price
        {"internet_number": A, "hd_store_id": "6381", "price": 999999.0},                          # too high
        {"internet_number": A, "hd_store_id": "6336", "price": 1.0, "observed_at": future},        # future
        {"internet_number": A, "hd_store_id": "0430", "price": 1.0, "original_price": 3.0, "observed_at": now},
    ])
    res = run_collection(internet_numbers=[A], provider=p)
    assert (res["rejected"], res["saved"]) == (3, 1)
    assert ph_count(platform.db) == 1
    reasons = " | ".join(e["detail"] for e in jobs.errors(platform.db) if e["status"] == "rejected")
    assert "lower than price" in reasons and "above" in reasons and "future" in reasons


def test_validate_unit():
    o = Observation(internet_number=A, hd_store_id="0260", price=1, original_price=2, source="t")
    assert validate(o, {"0260"}) == []
    assert "not in config" in validate(o, {"6381"})[0]
    short = Observation(internet_number="123", hd_store_id="0260", price=1, source="t")
    assert "9 or 10 digits" in validate(short, {"0260"})[0]


def test_daily_budget_stops_the_job(platform, monkeypatch):
    monkeypatch.setenv("COLLECTOR_MAX_PER_DAY", "2")
    p = MockProvider.from_file()
    res = run_collection(internet_numbers=[A], stores=["0260", "6336", "6381"], provider=p, notify=lambda m: None)
    assert res["stopped"] and len(p.requests) == 2 and res["saved"] == 2
    assert jobs.errors(platform.db)[0]["status"] == "budget"
    # the budget is in the database, so a new job the same day is refused straight away
    p2 = MockProvider.from_file()
    res2 = run_collection(internet_numbers=[B], provider=p2, notify=lambda m: None)
    assert res2["stopped"] and p2.requests == []


def test_provider_declared_limit_is_respected(platform):
    p = MockProvider.from_file()
    p.max_per_day = 1                                        # stricter than the env default
    res = run_collection(internet_numbers=[A], provider=p, notify=lambda m: None)
    assert len(p.requests) == 1 and res["stopped"]


def test_per_minute_budget_waits_instead_of_bursting(platform):
    t = [1_000_000.0]
    waits = []
    b = RequestBudget(platform.db, "x", per_minute=2, clock=lambda: t[0],
                      sleep=lambda s: (waits.append(s), t.__setitem__(0, t[0] + s)))
    assert b.acquire()[0] and b.acquire()[0]
    assert waits == []
    assert b.acquire()[0]
    assert len(waits) == 1 and 59 <= waits[0] <= 60


def test_cache_used_only_when_allowed(platform):
    p = MockProvider.from_file()
    run_collection(internet_numbers=[A], stores=["0260"], provider=p)
    res = run_collection(internet_numbers=[A], stores=["0260"], provider=p)
    assert len(p.requests) == 1 and res["cached"] == 1 and res["duplicates"] == 1
    assert ph_count(platform.db) == 1
    # a provider that doesn't allow caching is asked every time and nothing is cached
    q = MockProvider.from_file()
    q.cache_allowed = False
    q.name = "nocache"
    run_collection(internet_numbers=[B], stores=["0260"], provider=q)
    run_collection(internet_numbers=[B], stores=["0260"], provider=q)
    assert len(q.requests) == 2 and cache_get(platform.db, "nocache", B, "0260", 60) is None


def test_unverified_capabilities_are_labelled(platform):
    p = MockProvider([{"internet_number": A, "hd_store_id": "0260", "price": 5.0, "original_price": 10.0, "stock": 3}])
    p.capabilities = {}                                     # nothing verified
    p.mocked = False
    run_collection(internet_numbers=[A], stores=["0260"], provider=p)
    raw = json.loads(platform.db._q("SELECT raw FROM price_history")[0]["raw"])
    assert raw["price_scope"] == "unverified" and raw["inventory_verified"] is False and "mocked" not in raw


def test_price_change_counted(platform):
    t1 = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    t2 = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    p = MockProvider([{"internet_number": A, "hd_store_id": "0260",
                       "sequence": [{"price": 50.0, "original_price": 100.0, "observed_at": t1},
                                    {"price": 25.0, "original_price": 100.0, "observed_at": t2}]}])
    p.cache_allowed = False
    r1 = run_collection(internet_numbers=[A], stores=["0260"], provider=p)
    r2 = run_collection(internet_numbers=[A], stores=["0260"], provider=p)
    assert r1["price_changes"] == 0 and r2["price_changes"] == 1


def test_crash_marks_job_failed(platform):
    class Boom(MockProvider):
        def fetch(self, n, s):
            raise RuntimeError("kaboom")
    with pytest.raises(RuntimeError):
        run_collection(internet_numbers=[A], provider=Boom())
    assert jobs.recent(platform.db, 1)[0]["status"] == "failed"


def test_scrape_all_wrapper_keeps_old_contract(platform):
    db = platform.db
    db.upsert_product(A, source="test")
    res = platform.scraper.scrape_all(provider=MockProvider.from_file(), trigger="schedule")
    for k in ("ok", "fail", "blocked", "unavailable", "stopped", "provider"):
        assert k in res
    assert db.last_runs(1)[0]["job"] == "scrape"
    assert jobs.recent(db, 1)[0]["trigger"] == "schedule"
