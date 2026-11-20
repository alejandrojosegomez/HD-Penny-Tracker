"""Integration point for a licensed price/inventory data API.

Not wired to any vendor yet. Before filling this in, confirm in writing that the vendor's
terms allow store-level Home Depot price and inventory lookups for this use, and check how
many of your stores and markdowns it actually covers.

To add a vendor:
  1. Put its key in .env as PRICE_API_KEY (never in code or git) and its base URL in
     PRICE_API_URL.
  2. Implement _request() and _normalize() below for that vendor's documented API.
  3. Set PRICE_PROVIDER=licensed_api in .env.
  4. Add a test with a saved (fake) response in tests/fixtures/.
"""
from __future__ import annotations

import os
import time

from .base import Observation, ProviderResult, Status


class LicensedApiProvider:
    name = "licensed_api"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 min_interval: float | None = None):
        self.api_key = (api_key if api_key is not None else os.getenv("PRICE_API_KEY", "")).strip()
        self.base_url = (base_url if base_url is not None else os.getenv("PRICE_API_URL", "")).strip()
        # seconds between requests; set to whatever the vendor's plan allows
        self.min_interval = float(min_interval if min_interval is not None
                                  else os.getenv("PRICE_API_MIN_INTERVAL", "1"))

    def fetch(self, internet_number: str, store: dict) -> ProviderResult:
        if not self.api_key or not self.base_url:
            return ProviderResult(Status.ACCESS_DENIED, detail="PRICE_API_KEY / PRICE_API_URL not set")
        try:
            status_code, payload = self._request(internet_number, store)
        except NotImplementedError as e:
            return ProviderResult(Status.ERROR, detail=str(e))
        except Exception as e:      # network etc.
            return ProviderResult(Status.ERROR, detail=f"{type(e).__name__}: {e}")
        if status_code in (401, 403):
            return ProviderResult(Status.ACCESS_DENIED, detail=f"HTTP {status_code}")
        if status_code == 429:
            return ProviderResult(Status.RATE_LIMITED, detail="HTTP 429")
        if status_code == 404:
            return ProviderResult(Status.UNAVAILABLE, detail="HTTP 404")
        if status_code >= 400:
            return ProviderResult(Status.ERROR, detail=f"HTTP {status_code}")
        try:
            return ProviderResult.ok(self._normalize(internet_number, store, payload))
        except NotImplementedError as e:
            return ProviderResult(Status.ERROR, detail=str(e))
        except (KeyError, TypeError, ValueError) as e:
            return ProviderResult(Status.MALFORMED, detail=str(e))

    def pause(self) -> None:
        time.sleep(self.min_interval)

    # ---- fill these in for the chosen vendor -------------------------------
    def _request(self, internet_number: str, store: dict) -> tuple[int, dict]:
        """Call the vendor. Return (http_status, parsed_json)."""
        raise NotImplementedError("licensed_api: no vendor configured yet")

    def _normalize(self, internet_number: str, store: dict, payload: dict) -> Observation:
        """Map the vendor's JSON to an Observation(source=self.name, ...)."""
        raise NotImplementedError("licensed_api: no vendor configured yet")
