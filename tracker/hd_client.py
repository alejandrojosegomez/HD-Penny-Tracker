"""Talks to homedepot.com for one product in one store.

Order of attempts:
1. GraphQL `productClientOnlyProduct` with storeId + zipCode  (fast, store-specific)
2. Product page HTML: JSON-LD + window.__APOLLO_STATE__          (slower, still store-aware
   when the THD_LOCALIZER cookie points at the store)

Both return the same normalized dict:
    {
      "internet_number", "sku", "upc", "model", "brand", "name", "description",
      "image_url", "product_url", "price", "original_price", "stock", "availability",
      "discontinued", "raw_source"
    }
availability is one of: in_stock | limited | out_of_stock | unavailable | unknown
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from . import session as sess_mod
from .settings import settings

log = logging.getLogger("tracker.hd")

GRAPHQL_URL = "https://www.homedepot.com/federation-gateway/graphql?opname=productClientOnlyProduct"
PRODUCT_URL = "https://www.homedepot.com/p/{item}"
QUERY_PATH = Path(__file__).parent / "graphql" / "product.graphql"
BLOCK_MARKERS = ("errors.edgesuite.net", "Access Denied", "Reference #18.")


class Blocked(Exception):
    """Akamai refused us. Caller should refresh the session and back off."""


class HDClient:
    def __init__(self):
        self.sess = sess_mod.get_session()
        self.query = _load_query()
        self._client = self._make_client()
        self.blocks_in_a_row = 0

    # ---- plumbing ------------------------------------------------------
    def _make_client(self):
        c = cffi_requests.Session(impersonate="chrome")
        c.headers.update({
            "user-agent": self.sess.user_agent,
            "accept-language": "en-US,en;q=0.9",
            "origin": "https://www.homedepot.com",
            "referer": "https://www.homedepot.com/",
        })
        for k, v in self.sess.cookies.items():
            c.cookies.set(k, v, domain=".homedepot.com")
        return c

    def refresh_session(self) -> None:
        self.sess = sess_mod.get_session(force=True)
        self._client = self._make_client()

    def _sleep(self) -> None:
        time.sleep(random.uniform(settings.delay_min, settings.delay_max))

    def _localize(self, store_id: str, zip_code: str) -> None:
        self._client.cookies.set("THD_LOCALIZER", sess_mod._localizer_cookie(store_id, zip_code), domain=".homedepot.com")

    def _check_block(self, status: int, text: str) -> None:
        if status in (403, 429) or any(m in text[:2000] for m in BLOCK_MARKERS):
            self.blocks_in_a_row += 1
            raise Blocked(f"status={status}")
        self.blocks_in_a_row = 0

    # ---- public --------------------------------------------------------
    def fetch(self, internet_number: str, store_id: str, zip_code: str) -> dict:
        """Fetch one product for one store. Raises Blocked when Akamai says no."""
        self._localize(store_id, zip_code)
        try:
            data = self._fetch_graphql(internet_number, store_id, zip_code)
            if data:
                return data
        except Blocked:
            raise
        except Exception as e:  # schema drift, parsing, etc. -> fall back to HTML
            log.debug("graphql failed for %s: %s", internet_number, e)
        self._sleep()
        return self._fetch_html(internet_number, store_id)

    # ---- GraphQL -------------------------------------------------------
    def _fetch_graphql(self, item: str, store_id: str, zip_code: str) -> Optional[dict]:
        body = {
            "operationName": "productClientOnlyProduct",
            "variables": {"itemId": item, "storeId": store_id, "zipCode": zip_code},
            "query": self.query,
        }
        r = self._client.post(GRAPHQL_URL, json=body, headers={
            "content-type": "application/json",
            "x-experience-name": "general-merchandise",
            "x-hd-dc": "origin",
            "apollographql-client-name": "general-merchandise",
        }, timeout=30)
        self._check_block(r.status_code, r.text)
        payload = r.json()
        prod = (payload.get("data") or {}).get("product")
        if not prod:
            return None
        return _normalize_graphql(item, prod)

    # ---- HTML fallback -------------------------------------------------
    def _fetch_html(self, item: str, store_id: str) -> dict:
        r = self._client.get(PRODUCT_URL.format(item=item), timeout=40)
        self._check_block(r.status_code, r.text)
        return _normalize_html(item, r.text, store_id)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _load_query() -> str:
    text = QUERY_PATH.read_text()
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


def _availability_from_inventory(inv: dict | None) -> tuple[Optional[int], str]:
    if not inv:
        return None, "unknown"
    qty = inv.get("quantity")
    if inv.get("isUnavailable"):
        return qty, "unavailable"
    if inv.get("isOutOfStock") or (qty is not None and qty <= 0):
        return qty, "out_of_stock"
    if inv.get("isLimitedQuantity"):
        return qty, "limited"
    if inv.get("isInStock") or (qty is not None and qty > 0):
        return qty, "in_stock"
    return qty, "unknown"


def _normalize_graphql(item: str, p: dict) -> dict:
    ids = p.get("identifiers") or {}
    pricing = p.get("pricing") or {}
    media = ((p.get("media") or {}).get("images") or [{}])[0]
    img = media.get("url", "")
    if "<SIZE>" in img:
        sizes = media.get("sizes") or ["600"]
        img = img.replace("<SIZE>", str(sizes[-1]))

    stock, avail = None, "unknown"
    for opt in ((p.get("fulfillment") or {}).get("fulfillmentOptions") or []):
        for svc in opt.get("services") or []:
            if svc.get("type") not in ("bopis", "boss", "pickup"):
                continue
            for loc in svc.get("locations") or []:
                if loc.get("isAnchor") or stock is None:
                    stock, avail = _availability_from_inventory(loc.get("inventory"))
    discontinued = bool((p.get("availabilityType") or {}).get("discontinued"))
    if avail == "unknown" and discontinued:
        avail = "unavailable"

    url = ids.get("canonicalUrl") or f"/p/{item}"
    return {
        "internet_number": item,
        "sku": ids.get("storeSkuNumber"),
        "upc": ids.get("upc"),
        "model": ids.get("modelNumber"),
        "brand": ids.get("brandName"),
        "name": ids.get("productLabel"),
        "description": (p.get("details") or {}).get("description"),
        "image_url": img,
        "product_url": "https://www.homedepot.com" + url if url.startswith("/") else url,
        "price": pricing.get("value"),
        "original_price": pricing.get("original") or pricing.get("value"),
        "stock": stock,
        "availability": avail,
        "discontinued": discontinued,
        "raw_source": "graphql",
    }


def _normalize_html(item: str, html: str, store_id: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out = {"internet_number": item, "raw_source": "html", "availability": "unknown", "stock": None,
           "discontinued": False, "price": None, "original_price": None}

    # JSON-LD: name / brand / sku / gtin / price / availability (default store)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for d in items:
            if d.get("@type") != "Product":
                continue
            out["name"] = d.get("name")
            out["description"] = d.get("description")
            out["sku"] = d.get("sku")
            out["upc"] = d.get("gtin13") or d.get("gtin12") or d.get("gtin")
            out["model"] = d.get("model") or d.get("mpn")
            brand = d.get("brand")
            out["brand"] = brand.get("name") if isinstance(brand, dict) else brand
            img = d.get("image")
            out["image_url"] = img[0] if isinstance(img, list) else img
            out["product_url"] = d.get("url") or PRODUCT_URL.format(item=item)
            offers = d.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if offers.get("price") is not None:
                try:
                    out["price"] = float(offers["price"])
                except (TypeError, ValueError):
                    pass
            av = (offers.get("availability") or "").lower()
            if "instock" in av:
                out["availability"] = "in_stock"
            elif "outofstock" in av:
                out["availability"] = "out_of_stock"
            elif "discontinued" in av:
                out["availability"], out["discontinued"] = "unavailable", True

    # Apollo state: store-specific pricing/inventory when present
    m = re.search(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S)
    if m:
        try:
            apollo = json.loads(m.group(1))
            for key, val in apollo.items():
                if not isinstance(val, dict):
                    continue
                if key.startswith("Product:") or key.startswith("Pricing"):
                    for k2, v2 in val.items():
                        if k2.startswith("pricing") and store_id in k2 and isinstance(v2, dict):
                            out["price"] = v2.get("value", out["price"])
                            out["original_price"] = v2.get("original") or out["original_price"]
                        if k2.startswith("fulfillment") and store_id in k2 and isinstance(v2, dict):
                            for opt in v2.get("fulfillmentOptions") or []:
                                for svc in opt.get("services") or []:
                                    for loc in svc.get("locations") or []:
                                        if loc.get("isAnchor"):
                                            out["stock"], out["availability"] = _availability_from_inventory(loc.get("inventory"))
        except Exception as e:
            log.debug("apollo parse failed: %s", e)

    if out["original_price"] is None:
        out["original_price"] = out["price"]
    return out
