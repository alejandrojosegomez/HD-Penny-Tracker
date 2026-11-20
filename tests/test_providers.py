"""Provider interface: normalized observations, statuses, and a scraper that stops on refusals."""
import importlib
import json

import pytest

from tracker.providers.base import Observation, ProviderResult, Status
from tracker.providers.licensed_api import LicensedApiProvider
from tracker.providers.sample import SampleProvider


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_MODE", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(k, "")
    import tracker.settings as s; importlib.reload(s)
    import tracker.storage as st; importlib.reload(st)
    import tracker.notify as nt; importlib.reload(nt)
    import tracker.scraper as sc; importlib.reload(sc)
    sent = []
    monkeypatch.setattr(sc, "send", lambda m: sent.append(m) or True)
    monkeypatch.setattr(sc, "read_watchlist", lambda: [])
    db = st.get_storage()
    for x in s.settings.stores:
        db.upsert_store(x.store_id, x.name, x.city, x.zip)
    return sc, db, sent


def test_observation_normalizes_and_validates():
    o = Observation(internet_number=" 1006423739 ", hd_store_id="260", price="0.060", source="t",
                    availability="weird")
    assert (o.internet_number, o.hd_store_id, o.price, o.availability) == ("1006423739", "0260", 0.06, "unknown")
    for bad in ({"internet_number": "abc"}, {"hd_store_id": "12345"}, {"price": -1}, {"source": ""}):
        with pytest.raises(ValueError):
            Observation(**{"internet_number": "1", "hd_store_id": "0260", "price": 1, "source": "t", **bad})


def test_refusals_stop_the_run():
    assert Status.ACCESS_DENIED.stops_run and Status.RATE_LIMITED.stops_run
    assert not any(s.stops_run for s in (Status.OK, Status.UNAVAILABLE, Status.MALFORMED, Status.ERROR))


def test_sample_provider_statuses():
    p = SampleProvider()
    assert p.fetch("1006423739", {"hd_store_id": "0260"}).observation.price == 0.06
    assert p.fetch("1004871058", {"hd_store_id": "6381"}).status is Status.RATE_LIMITED
    assert p.fetch("999999999", {"hd_store_id": "0260"}).status is Status.UNAVAILABLE


def test_sample_provider_reports_malformed_rows(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps([{"internet_number": "x1", "hd_store_id": "0260", "price": 1}]))
    assert SampleProvider(f).fetch("x1", {"hd_store_id": "0260"}).status is Status.MALFORMED


def test_licensed_api_without_key_is_denied_not_retried():
    r = LicensedApiProvider(api_key="", base_url="").fetch("1006423739", {"hd_store_id": "0260"})
    assert r.status is Status.ACCESS_DENIED and "PRICE_API_KEY" in r.detail


def test_licensed_api_maps_http_statuses():
    class Fake(LicensedApiProvider):
        code = 200
        def _request(self, n, store): return self.code, {"p": 1.0}
        def _normalize(self, n, store, payload):
            return Observation(internet_number=n, hd_store_id=store["hd_store_id"], price=payload["p"], source=self.name)
    p = Fake(api_key="k", base_url="https://example.invalid", min_interval=0)
    store = {"hd_store_id": "0260"}
    assert p.fetch("1", store).status is Status.OK
    for code, st in ((401, Status.ACCESS_DENIED), (429, Status.RATE_LIMITED), (404, Status.UNAVAILABLE), (500, Status.ERROR)):
        p.code = code
        assert p.fetch("1", store).status is st


def test_scraper_saves_sample_observations(fresh):
    sc, db, sent = fresh
    for n in ("1006423739", "1004871058"):
        db.upsert_product(n, source="test")
    p = SampleProvider()
    calls = []
    p.fetch = (lambda f: lambda n, s: calls.append((n, s["hd_store_id"])) or f(n, s))(p.fetch)
    res = sc.scrape_all(provider=p)
    # 1006423739 x 2 stores saved, 1004871058 @ 0260 saved, then 1004871058 @ 6381 is rate limited -> stop
    assert res["stopped"] and res["ok"] == 3 and res["blocked"] == 1
    assert calls.index(("1004871058", "6381")) == len(calls) - 1     # nothing requested after the refusal
    assert sent and "rate_limited" in sent[0]
    pid = db.product_by_internet_number("1006423739")["id"]
    st = next(s for s in db.stores() if s["hd_store_id"] == "0260")
    row = db.history(pid, st["id"])[-1]
    assert (row["price"], row["original_price"], row["availability"]) == (0.06, 129.0, "limited")
    assert json.loads(row["raw"])["source"] == "sample"
    assert db.product_by_internet_number("1006423739")["name"].startswith("Hazel Heights")
