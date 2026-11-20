"""The original direct client (tracker/hd_client.py) behind the provider interface.

Kept only so existing setups behave as before. Home Depot's Akamai Bot Manager blocks it;
a block is reported as ACCESS_DENIED and the run stops. Nothing here tries to get around
that. Prefer manual entry or a licensed provider.
"""
from __future__ import annotations

from .base import Observation, ProviderResult, Status


class HDDirectProvider:
    name = "hd_direct"

    def __init__(self):
        from ..hd_client import HDClient      # imports the browser stack only when chosen
        self._client = HDClient()

    def fetch(self, internet_number: str, store: dict) -> ProviderResult:
        from ..hd_client import Blocked
        try:
            d = self._client.fetch(internet_number, store["hd_store_id"], store.get("zip") or "")
        except Blocked as e:
            return ProviderResult(Status.ACCESS_DENIED, detail=str(e))
        except Exception as e:
            return ProviderResult(Status.ERROR, detail=f"{type(e).__name__}: {e}")
        try:
            obs = Observation(
                internet_number=internet_number, hd_store_id=store["hd_store_id"],
                price=d.get("price"), original_price=d.get("original_price"), stock=d.get("stock"),
                availability=d.get("availability") or "unknown", source=self.name,
                name=d.get("name"), sku=d.get("sku"), upc=d.get("upc"), model=d.get("model"),
                brand=d.get("brand"), description=d.get("description"), image_url=d.get("image_url"), product_url=d.get("product_url"),
                extra={"raw_source": d.get("raw_source"), "discontinued": d.get("discontinued")})
        except (TypeError, ValueError) as e:
            return ProviderResult(Status.MALFORMED, detail=str(e))
        return ProviderResult.ok(obs)

    def pause(self) -> None:
        self._client._sleep()
