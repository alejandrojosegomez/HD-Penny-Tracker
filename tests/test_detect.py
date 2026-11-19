"""Scoring + parser tests. Run: python -m pytest -q"""
from datetime import datetime, timedelta, timezone

from tracker.detect import score_history
from tracker.hd_client import _normalize_html, _normalize_graphql


def _h(days_ago, price, orig, avail="in_stock", stock=3):
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    disc = round(100 * (1 - price / orig), 2) if orig else None
    return {"scraped_at": t.isoformat(), "price": price, "original_price": orig,
            "discount_pct": disc, "availability": avail, "stock": stock, "raw": ""}


def test_full_price_scores_zero():
    s, r = score_history([_h(1, 100, 100)], None, [])
    assert s == 0 and r == []


def test_three_markdowns_deep_and_dwell():
    hist = [_h(60, 100, 100), _h(45, 50, 100), _h(30, 25, 100)] + [_h(d, 9.03, 100) for d in range(20, -1, -1)]
    s, r = score_history(hist, "patio", [])
    # 30 (≥90%) + 25 (3 markdowns) + 15 (>14d dwell) + 10 seasonal + 5 ending = 85
    assert s == 85, (s, r)
    assert any("markdowns" in x for x in r)


def test_zma_flip():
    hist = [_h(5, 4.02, 100), _h(3, 4.02, 100), _h(0, 100, 100, avail="unavailable", stock=0)]
    s, r = score_history(hist, None, [])
    assert s >= 40 and any("ZMA" in x for x in r)


def test_community_with_stock():
    hist = [_h(0, 12.00, 60)]
    s, r = score_history(hist, None, [{"source": "pennycentral"}])
    assert s == 35 + 15 and "penny list" in " ".join(r)


def test_html_parser_jsonld():
    html = """<html><script type="application/ld+json">
    {"@type":"Product","name":"Patio Chair","sku":"1001234567","brand":{"name":"Hampton Bay"},
     "image":["https://img/1.jpg"],"offers":{"price":"49.03","availability":"https://schema.org/InStock"}}
    </script></html>"""
    d = _normalize_html("203401615", html, "0260")
    assert d["name"] == "Patio Chair" and d["brand"] == "Hampton Bay"
    assert d["price"] == 49.03 and d["availability"] == "in_stock"


def test_graphql_normalize():
    prod = {"identifiers": {"storeSkuNumber": "1003", "upc": "0123", "productLabel": "Vanity", "brandName": "Glacier Bay",
                            "canonicalUrl": "/p/x/203401615"},
            "pricing": {"value": 2.02, "original": 289.0},
            "media": {"images": [{"url": "https://img/<SIZE>.jpg", "sizes": ["100", "600"]}]},
            "fulfillment": {"fulfillmentOptions": [{"services": [{"type": "bopis", "locations": [
                {"isAnchor": True, "inventory": {"quantity": 2, "isInStock": True}}]}]}]},
            "availabilityType": {"discontinued": True}}
    d = _normalize_graphql("203401615", prod)
    assert d["price"] == 2.02 and d["original_price"] == 289.0
    assert d["stock"] == 2 and d["availability"] == "in_stock" and d["discontinued"]
    assert d["image_url"].endswith("600.jpg")


def test_alert_escapes_html():
    from tracker.notify import format_alert
    msg = format_alert({"internet_number": "318430226", "name": "BLACK+DECKER Drill & <Bits>"},
                       {"name": "West Melbourne", "hd_store_id": "0260"}, {"price": 0.01}, 80, ["a & b"])
    assert "Drill &amp; &lt;Bits&gt;" in msg and "a &amp; b" in msg and "<Bits>" not in msg


def test_telegram_token_fallback(monkeypatch):
    import importlib
    import tracker.settings as s
    # empty, not deleted: a deleted var gets refilled from a local .env when settings reloads
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_TOKEN", "abc")
    importlib.reload(s)
    assert s.settings.telegram_token == "abc"
    monkeypatch.delenv("TELEGRAM_TOKEN")
    importlib.reload(s)
