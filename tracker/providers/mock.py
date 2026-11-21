"""A scriptable fake provider. No network, ever.

Use it to develop and test the collector and the API before a real, permitted source is
connected. Every observation it returns has source="mock", so mock data is never mistaken
for real prices.

Responses come from a JSON file (default: tracker/providers/fixtures/mock_responses.json,
or MOCK_PROVIDER_FILE) or are passed in directly:

    {
      "skus": {"1001234567": "1006423739"},
      "observations": [
        {"internet_number": "1006423739", "hd_store_id": "0260", "price": 0.06, "original_price": 129.0},
        {"internet_number": "1004871058", "hd_store_id": "6381", "status": "rate_limited"}
      ]
    }

A row may carry "status" to simulate any providers.Status, and "sequence": [ ...rows ] to
answer differently on each call (the last entry repeats).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .base import Observation, ProviderResult, Status

DEFAULT_PATH = Path(__file__).parent / "fixtures" / "mock_responses.json"


class MockProvider:
    name = "mock"
    mocked = True
    cache_allowed = True
    capabilities = {"store_level_price": True, "clearance_price": True, "inventory": True, "sku_lookup": True}

    def __init__(self, observations: Optional[list[dict]] = None, skus: Optional[dict] = None,
                 latency: float = 0.0, max_per_minute: int = 0, max_per_day: int = 0):
        self.latency = latency
        self.max_per_minute = max_per_minute
        self.max_per_day = max_per_day
        self.skus = {str(k): str(v) for k, v in (skus or {}).items()}
        self._rows: dict[tuple[str, str], dict] = {}
        self._calls: dict[tuple[str, str], int] = {}
        self.requests: list[tuple[str, str]] = []          # every fetch, for tests
        for r in observations or []:
            self._rows[(str(r["internet_number"]), str(r["hd_store_id"]).zfill(4))] = r

    @classmethod
    def from_file(cls, path: Path | str | None = None) -> "MockProvider":
        p = Path(path) if path else DEFAULT_PATH
        data = json.loads(p.read_text()) if p.exists() else {}
        return cls(data.get("observations", []), data.get("skus", {}))

    def lookup_sku(self, sku: str) -> Optional[str]:
        return self.skus.get(str(sku).strip())

    def fetch(self, internet_number: str, store: dict) -> ProviderResult:
        key = (str(internet_number), str(store["hd_store_id"]).zfill(4))
        self.requests.append(key)
        if self.latency:
            time.sleep(self.latency)
        row = self._rows.get(key)
        if row is None:
            return ProviderResult(Status.UNAVAILABLE, detail="no mock response for this product/store")
        if "sequence" in row:
            i = self._calls.get(key, 0)
            self._calls[key] = i + 1
            seq = row["sequence"]
            row = {"internet_number": key[0], "hd_store_id": key[1], **seq[min(i, len(seq) - 1)]}
        status = row.get("status", "ok")
        if status != "ok":
            return ProviderResult(Status(status), detail=row.get("detail", "mock status"))
        try:
            data = {k: v for k, v in row.items() if k not in ("status", "detail", "sequence")}
            return ProviderResult.ok(Observation(source=self.name, **data))
        except (TypeError, ValueError) as e:
            return ProviderResult(Status.MALFORMED, detail=str(e))

    def pause(self) -> None:
        pass
