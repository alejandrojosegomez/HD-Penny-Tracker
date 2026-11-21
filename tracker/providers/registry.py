"""Pick a provider by name (PRICE_PROVIDER in .env)."""
from __future__ import annotations

import os


def available() -> list[str]:
    return ["hd_direct", "licensed_api", "sample", "mock"]


def get_provider(name: str | None = None):
    name = (name or os.getenv("PRICE_PROVIDER", "hd_direct")).strip().lower()
    if name == "sample":
        from .sample import SampleProvider
        return SampleProvider(os.getenv("SAMPLE_OBSERVATIONS") or None)
    if name == "mock":
        from .mock import MockProvider
        return MockProvider.from_file(os.getenv("MOCK_PROVIDER_FILE") or None)
    if name == "licensed_api":
        from .licensed_api import LicensedApiProvider
        return LicensedApiProvider()
    if name == "hd_direct":
        from .hd_direct import HDDirectProvider
        return HDDirectProvider()
    raise ValueError(f"unknown PRICE_PROVIDER {name!r}; choose one of {available()}")
