"""Price/inventory data providers.

Every source of store prices (manual entry, sample fixtures, a licensed data API, the old
direct client) returns the same normalized `Observation`, so the scraper, detector and
alerts never depend on one website or vendor. See tracker/providers/README.md.
"""
from .base import Observation, Provider, ProviderResult, Status
from .registry import available, get_provider

__all__ = ["Observation", "Provider", "ProviderResult", "Status", "available", "get_provider"]
