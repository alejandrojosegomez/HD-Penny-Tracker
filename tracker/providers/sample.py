"""Reads observations from a JSON file. For development, demos and tests: no network."""
from __future__ import annotations

import json
from pathlib import Path

from .base import Observation, ProviderResult, Status

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sample_observations.json"


class SampleProvider:
    name = "sample"

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_PATH
        rows = json.loads(self.path.read_text()) if self.path.exists() else []
        self._rows = {(str(r["internet_number"]), str(r["hd_store_id"]).zfill(4)): r for r in rows}

    def fetch(self, internet_number: str, store: dict) -> ProviderResult:
        row = self._rows.get((str(internet_number), str(store["hd_store_id"]).zfill(4)))
        if row is None:
            return ProviderResult(Status.UNAVAILABLE, detail="not in sample file")
        if row.get("status") and row["status"] != "ok":
            return ProviderResult(Status(row["status"]), detail="sample status")
        try:
            data = {k: v for k, v in row.items() if k != "status"}
            return ProviderResult.ok(Observation(source=self.name, **data))
        except (TypeError, ValueError) as e:
            return ProviderResult(Status.MALFORMED, detail=str(e))

    def pause(self) -> None:
        pass
