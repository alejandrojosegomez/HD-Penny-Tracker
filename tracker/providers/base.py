"""The contract every provider follows."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol


class Status(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"          # provider has no data for this product/store
    RATE_LIMITED = "rate_limited"        # provider asked us to slow down
    ACCESS_DENIED = "access_denied"      # provider refused us (bad key, blocked, not permitted)
    MALFORMED = "malformed"              # answer came back but could not be understood
    ERROR = "error"                      # network / unexpected failure

    @property
    def stops_run(self) -> bool:
        """Refusals end the run. Never answer a refusal with more requests."""
        return self in (Status.RATE_LIMITED, Status.ACCESS_DENIED)


AVAILABILITY = {"in_stock", "out_of_stock", "limited", "unknown"}


@dataclass
class Observation:
    """One price seen for one product at one store, from one source."""
    internet_number: str
    hd_store_id: str
    price: Optional[float]
    source: str
    original_price: Optional[float] = None
    stock: Optional[int] = None
    availability: str = "unknown"
    observed_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    # optional product details a provider may know
    name: Optional[str] = None
    sku: Optional[str] = None
    upc: Optional[str] = None
    model: Optional[str] = None
    brand: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    product_url: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.internet_number = str(self.internet_number).strip()
        self.hd_store_id = str(self.hd_store_id).strip().zfill(4)
        if not self.internet_number.isdigit():
            raise ValueError(f"internet_number must be digits: {self.internet_number!r}")
        if not self.hd_store_id.isdigit() or len(self.hd_store_id) != 4:
            raise ValueError(f"hd_store_id must be 4 digits: {self.hd_store_id!r}")
        for f in ("price", "original_price"):
            v = getattr(self, f)
            if v is not None:
                v = round(float(v), 2)
                if v < 0:
                    raise ValueError(f"{f} cannot be negative")
                setattr(self, f, v)
        if self.stock is not None:
            self.stock = int(self.stock)
        if self.availability not in AVAILABILITY:
            self.availability = "unknown"
        if not self.source:
            raise ValueError("source is required")

    def product_fields(self) -> dict:
        keys = ("name", "sku", "upc", "model", "brand", "description", "image_url", "product_url")
        return {k: getattr(self, k) for k in keys if getattr(self, k)}

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderResult:
    status: Status
    observation: Optional[Observation] = None
    detail: str = ""

    @classmethod
    def ok(cls, obs: Observation) -> "ProviderResult":
        return cls(Status.OK, obs)


# What a provider is verified to return. Everything defaults to False: a source is not
# assumed to give store-level clearance prices or inventory until that has been checked.
CAPABILITIES = ("store_level_price", "clearance_price", "inventory", "sku_lookup")


def capabilities_of(provider) -> dict:
    declared = getattr(provider, "capabilities", None) or {}
    return {k: bool(declared.get(k, False)) for k in CAPABILITIES}


class Provider(Protocol):
    """Required: name, fetch(), pause().

    Optional attributes the collector reads with getattr():
      capabilities: dict    see CAPABILITIES; anything missing counts as False
      cache_allowed: bool   True only when the source's terms allow keeping responses
      mocked: bool          True for fixtures / fakes (never real data)
      max_per_minute / max_per_day: int   the source's own request limits (0 = none)
      lookup_sku(sku) -> str | None       internet number for a store SKU
    """
    name: str

    def fetch(self, internet_number: str, store: dict) -> ProviderResult:
        """store: a row from storage.stores() (hd_store_id, name, zip...)."""
        ...

    def pause(self) -> None:
        """Wait between requests, as the provider's terms require. No-op if none."""
        ...
