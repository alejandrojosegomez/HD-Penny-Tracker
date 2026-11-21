"""The collection engine: products x stores -> provider -> validate -> save.

This is the loop that used to live in scraper.py, generalized so a job can target the whole
watchlist, a few products, SKUs or stores, and so every job is recorded with its counts.
scraper.scrape_all() still works exactly as before and runs through here.

Rules it keeps:
* a refusal (access_denied / rate_limited) ends the job at once: no retries, one Telegram notice
* the request budget (limits.py) is checked before every request
* cached answers are reused only for providers that allow caching
* every observation is validated, then saved through providers.save.save_observation
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Callable, Iterable, Optional

from .. import settings as settings_mod
from .. import storage as storage_mod
from ..providers import Status, get_provider
from ..providers.base import capabilities_of
from ..providers.save import save_observation
from . import cache, config, jobs
from .limits import RequestBudget
from .validate import validate

log = logging.getLogger("tracker.collector")


@contextmanager
def collection_lock():
    """One collection at a time across processes/containers (worker schedule, dashboard
    button and API share data/). Yields False if another one is already running."""
    try:
        import fcntl
    except ImportError:          # Windows dev box: single process, nothing to coordinate
        yield True
        return
    path = settings_mod.settings.sqlite_path.parent / "scrape.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class TargetError(ValueError):
    """A requested product, SKU or store can't be collected (shown to the caller as is)."""


def resolve_stores(db, stores: Optional[Iterable[str]]) -> list[dict]:
    all_stores = db.stores()
    if not stores:
        return all_stores
    wanted = [str(s).strip().zfill(4) for s in stores]
    known = {s["hd_store_id"] for s in all_stores}
    unknown = [s for s in wanted if s not in known]
    if unknown:
        raise TargetError(f"unknown store(s) {', '.join(unknown)}; configured: {', '.join(sorted(known))}")
    return [s for s in all_stores if s["hd_store_id"] in wanted]


def _valid_number(n: str) -> str:
    n = str(n).strip()
    if not n.isdigit() or len(n) not in (9, 10):
        raise TargetError(f"internet number must be 9 or 10 digits: {n!r}")
    return n


def run_collection(internet_numbers: Optional[Iterable[str]] = None, skus: Optional[Iterable[str]] = None,
                   stores: Optional[Iterable[str]] = None, provider=None, trigger: str = "manual",
                   limit: Optional[int] = None, job_id: Optional[int] = None,
                   notify: Optional[Callable[[str], object]] = None, sync: Optional[Callable[[], int]] = None,
                   run_name: str = "collect") -> dict:
    """Run one collection job. Returns a summary dict (also stored in collection_jobs).

    No targets = every active product (after syncing the watchlist) at every store.
    """
    db = storage_mod.get_storage()
    provider = provider or get_provider()
    params = {"internet_numbers": list(internet_numbers or []), "skus": list(skus or []),
              "stores": list(stores or []), "limit": limit}
    if job_id is None:
        job_id = jobs.create(db, trigger, provider.name, params)
    with collection_lock() as got:
        if not got:
            log.warning("job %s: another collection is already running; skipping", job_id)
            jobs.finish(db, job_id, "skipped", {}, "another collection was already running")
            return {"job_id": job_id, "ok": 0, "fail": 0, "blocked": 0, "stopped": True,
                    "skipped": "already running", "provider": provider.name}
        return _run(db, provider, job_id, params, trigger, notify, sync, run_name)


def _run(db, provider, job_id, params, trigger, notify, sync, run_name) -> dict:
    jobs.start(db, job_id)
    run_id = db.start_run(run_name)
    counts = {s.value: 0 for s in Status}
    counts.update({c: 0 for c in jobs.COUNTERS})
    notify = notify or (lambda m: None)
    caps = capabilities_of(provider)
    mocked = bool(getattr(provider, "mocked", False))
    budget = RequestBudget(db, provider.name,
                           config.effective_limit(config.max_per_minute(), getattr(provider, "max_per_minute", 0)),
                           config.effective_limit(config.max_per_day(), getattr(provider, "max_per_day", 0)))
    ttl = config.cache_ttl_minutes() if cache.allowed(provider) else 0
    seen_products: set[str] = set()
    seen_stores: set[str] = set()

    def stop(status: str, msg: str) -> dict:
        log.error(msg)
        notify(msg)
        out = _summary(job_id, counts, True, provider.name, seen_products, seen_stores, detail=msg)
        jobs.finish(db, job_id, status, out, msg)
        db.finish_run(run_id, False, msg)
        return out

    try:
        stores = resolve_stores(db, params["stores"])
        targets: list[str] = []
        for n in params["internet_numbers"]:
            targets.append(_valid_number(n))
        for sku in params["skus"]:
            n = _resolve_sku(db, provider, budget, counts, job_id, str(sku).strip())
            if n:
                targets.append(n)
        if not params["internet_numbers"] and not params["skus"]:
            if sync:
                sync()
            targets = [p["internet_number"] for p in db.products()]
        targets = list(dict.fromkeys(targets))           # de-dupe, keep order
        if params.get("limit"):
            targets = targets[:params["limit"]]
    except TargetError as e:
        out = _summary(job_id, counts, True, provider.name, seen_products, seen_stores, detail=str(e))
        out["error"] = str(e)
        jobs.finish(db, job_id, "failed", out, str(e))
        db.finish_run(run_id, False, str(e))
        return out

    log.info("job %s (%s): %d products x %d stores with provider %s%s", job_id, trigger, len(targets),
             len(stores), provider.name, " [mock data]" if mocked else "")
    known_store_ids = {s["hd_store_id"] for s in db.stores()}
    try:
        for n in targets:
            for s in stores:
                seen_products.add(n)
                seen_stores.add(s["hd_store_id"])
                t0 = time.monotonic()
                obs = cache.get(db, provider.name, n, s["hd_store_id"], ttl) if ttl else None
                if obs is not None:
                    counts["cached"] += 1
                    counts["ok"] += 1
                    status, detail = Status.OK, "cache"
                else:
                    allowed, why = budget.acquire()
                    if not allowed:
                        jobs.record_error(db, job_id, provider.name, "budget", n, s["hd_store_id"], why)
                        counts["rate_limited"] += 1
                        return stop("stopped", f"HD Penny Tracker: {provider.name} stopped by the local request "
                                               f"budget: {why}. {counts['saved']} saved this run.")
                    counts["requests"] += 1
                    res = provider.fetch(n, s)
                    status, detail, obs = res.status, res.detail, res.observation
                    counts[status.value] += 1
                log.info("job=%s provider=%s item=%s store=%s status=%s cached=%s ms=%d %s", job_id, provider.name,
                         n, s["hd_store_id"], status.value, detail == "cache",
                         (time.monotonic() - t0) * 1000, detail if status is not Status.OK else "")

                if status.stops_run:
                    jobs.record_error(db, job_id, provider.name, status.value, n, s["hd_store_id"], detail)
                    # a refusal ends the run: no retries, no extra requests until the next schedule
                    return stop("stopped",
                                f"HD Penny Tracker: {provider.name} answered {status.value} ({detail}) "
                                f"on {n} @ {s['hd_store_id']}. Stopping this run; "
                                f"{counts['ok']} saved. Manual prices still work.")
                if status is Status.OK:
                    _handle_ok(db, provider, obs, caps, mocked, job_id, known_store_ids, counts,
                               cache_it=ttl > 0 and detail != "cache")
                else:
                    # "unavailable" was already counted above (same key as the Status value)
                    if status is not Status.UNAVAILABLE:
                        jobs.record_error(db, job_id, provider.name, status.value, n, s["hd_store_id"], detail)
                    log.warning("%s @ %s: %s %s", n, s["hd_store_id"], status.value, detail)
                if detail != "cache":
                    provider.pause()
    except Exception as e:
        msg = f"job {job_id} crashed: {type(e).__name__}: {e}"
        log.exception(msg)
        jobs.record_error(db, job_id, provider.name, "error", detail=msg)
        jobs.finish(db, job_id, "failed",
                    _summary(job_id, counts, True, provider.name, seen_products, seen_stores), msg)
        db.finish_run(run_id, False, msg)
        raise

    out = _summary(job_id, counts, False, provider.name, seen_products, seen_stores)
    summary = (f"{run_name} done ({provider.name}): {out['ok']} ok, {out['fail']} failed, "
               f"{out['unavailable']} unavailable, {out['rejected']} rejected, {out['price_changes']} price changes")
    jobs.finish(db, job_id, "succeeded", out, summary)
    db.finish_run(run_id, True, summary)
    log.info("job %s: %s", job_id, summary)
    return out


def _resolve_sku(db, provider, budget, counts, job_id, sku: str) -> Optional[str]:
    p = db.product_by_sku(sku)
    if p:
        return p["internet_number"]
    lookup = getattr(provider, "lookup_sku", None)
    if lookup and capabilities_of(provider)["sku_lookup"]:
        allowed, why = budget.acquire()
        if not allowed:
            raise TargetError(f"can't look up SKU {sku}: {why}")
        counts["requests"] += 1
        n = lookup(sku)
        if n:
            db.upsert_product(n, sku=sku, source=provider.name)
            return n
    jobs.record_error(db, job_id, provider.name, "unavailable", detail=f"unknown SKU {sku}")
    counts["unavailable"] += 1
    log.warning("job %s: SKU %s is not a known product and %s can't look it up", job_id, sku, provider.name)
    return None


def _handle_ok(db, provider, obs, caps, mocked, job_id, known_store_ids, counts, cache_it: bool) -> None:
    problems = validate(obs, known_store_ids)
    if problems:
        counts["rejected"] += 1
        counts["ok"] -= 1
        detail = "; ".join(problems)
        jobs.record_error(db, job_id, provider.name, "rejected", obs.internet_number, obs.hd_store_id, detail)
        log.warning("job=%s rejected %s @ %s: %s", job_id, obs.internet_number, obs.hd_store_id, detail)
        return
    # attribution: who said this, in which job, and how much of it is verified
    obs.extra.update({"job_id": job_id, "provider": provider.name,
                      "price_scope": "store" if caps["store_level_price"] else "unverified",
                      "inventory_verified": caps["inventory"]})
    if mocked:
        obs.extra["mocked"] = True
    res = save_observation(obs, db, details=True)
    if res.created:
        counts["saved"] += 1
        if res.price_changed:
            counts["price_changes"] += 1
    else:
        counts["duplicates"] += 1
    if cache_it:
        cache.put(db, provider.name, obs)


def _summary(job_id, counts: dict, stopped: bool, provider: str, products: set, stores: set,
             detail: str = "") -> dict:
    out = {"job_id": job_id,
           "ok": counts["ok"],
           "fail": counts["error"] + counts["malformed"],
           "blocked": counts["access_denied"] + counts["rate_limited"],
           "unavailable": counts["unavailable"],
           "stopped": stopped, "provider": provider,
           "products_processed": len(products), "stores_processed": len(stores),
           "errors": counts["error"] + counts["malformed"] + counts["access_denied"] + counts["rate_limited"]}
    for k in ("requests", "cached", "saved", "duplicates", "rejected", "price_changes"):
        out[k] = counts[k]
    if detail:
        out["detail"] = detail
    return out
