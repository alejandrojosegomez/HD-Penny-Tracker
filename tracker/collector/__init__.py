"""Collection engine: runs providers over products x stores, validates and saves the results.

    from tracker.collector import run_collection
    run_collection()                                   # whole watchlist, every store
    run_collection(internet_numbers=["1006423739"])    # one product
    run_collection(skus=["1001234567"], stores=["0260"])

See docs/PLATFORM.md.
"""
from .engine import TargetError, collection_lock, run_collection

__all__ = ["TargetError", "collection_lock", "run_collection"]
