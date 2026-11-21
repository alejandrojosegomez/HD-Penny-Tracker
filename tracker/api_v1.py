"""Authenticated JSON API (/v1) for other apps: products, stores, price history, observation
submission, collection jobs, monitoring.

Every route except /v1/health needs an `X-API-Key` header with a key from API_KEYS in .env
(`name:key,name2:key2`). The key's name is used as the source of anything submitted with it
and to count API usage. Reads come from the same database the dashboard uses; writes go
through providers.save.save_observation, the only place price rows are written.

The unauthenticated /api/* routes in web.py are the local dashboard's and are unchanged.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from . import __version__
from . import settings as settings_mod
from . import storage as storage_mod

UTC = timezone.utc
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="API key from API_KEYS in .env")


# ---------------------------------------------------------------------------
# auth + usage
# ---------------------------------------------------------------------------
def _db():
    return storage_mod.get_storage()


def require_key(request: Request, key: Optional[str] = Security(api_key_header)) -> str:
    keys = settings_mod.api_keys()
    if not keys:
        raise HTTPException(503, "API keys are not configured (set API_KEYS in .env)")
    if not key:
        raise HTTPException(401, "missing X-API-Key header", headers={"WWW-Authenticate": "API-Key"})
    name = None
    for k, n in keys.items():                 # compare against every key: no early exit
        if hmac.compare_digest(k.encode(), key.encode()):
            name = n
    if name is None:
        raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "API-Key"})
    now = datetime.now(UTC)
    _db()._x("INSERT INTO api_usage(key_name, day, requests, last_path, last_at) VALUES(?,?,1,?,?) "
             "ON CONFLICT(key_name, day) DO UPDATE SET requests=requests+1, last_path=excluded.last_path, "
             "last_at=excluded.last_at",
             (name, now.date().isoformat(), f"{request.method} {request.url.path}", now.isoformat(timespec="seconds")))
    return name


router = APIRouter(prefix="/v1")
secured = APIRouter(dependencies=[Depends(require_key)])


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------
class V1Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: Literal["ok", "error"]
    database_mode: str
    price_provider: str
    scrape_enabled: bool
    api_keys_configured: bool
    last_successful_collection: Optional[str] = None
    time: str


class V1Store(BaseModel):
    hd_store_id: str
    name: Optional[str] = None
    city: Optional[str] = None
    zip: Optional[str] = None


class V1Price(BaseModel):
    hd_store_id: str
    store_name: Optional[str] = None
    price: Optional[float] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    stock: Optional[int] = None
    availability: Optional[str] = None
    recorded_at: str = Field(description="when the row was stored")
    observed_at: Optional[str] = Field(None, description="when the source saw the price")
    source: Optional[str] = None
    price_scope: Optional[str] = Field(None, description="store = provider verified store-level prices; unverified otherwise")
    mocked: bool = False


class V1Product(BaseModel):
    internet_number: str
    sku: Optional[str] = None
    upc: Optional[str] = None
    model: Optional[str] = None
    name: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    product_url: Optional[str] = None
    source: Optional[str] = None
    active: bool = True
    created_at: Optional[str] = None


class V1ProductDetail(V1Product):
    latest: list[V1Price]


class V1StorePrice(V1Price):
    internet_number: str
    name: Optional[str] = None


class V1History(BaseModel):
    internet_number: str
    days: int
    stores: dict[str, list[V1Price]]


class V1ObservationIn(BaseModel):
    internet_number: str = Field(examples=["1006423739"])
    hd_store_id: str = Field(examples=["0260"])
    price: Optional[float] = Field(None, ge=0, examples=[0.06])
    original_price: Optional[float] = Field(None, ge=0, examples=[129.0])
    stock: Optional[int] = Field(None, ge=0)
    availability: Literal["in_stock", "out_of_stock", "limited", "unknown"] = "unknown"
    observed_at: Optional[str] = Field(None, description="ISO time the price was seen; default now")
    sku: Optional[str] = None
    upc: Optional[str] = None
    model: Optional[str] = None
    name: Optional[str] = None
    brand: Optional[str] = None
    image_url: Optional[str] = None
    product_url: Optional[str] = None
    note: Optional[str] = Field(None, max_length=500, description="free text kept with the row")


class V1ObservationOut(BaseModel):
    id: int
    created: bool = Field(description="false = identical observation already stored; nothing was added")
    price_changed: bool
    previous_price: Optional[float] = None
    source: str


class V1CollectIn(BaseModel):
    internet_numbers: list[str] = Field(default_factory=list)
    skus: list[str] = Field(default_factory=list)
    stores: list[str] = Field(default_factory=list, description="hd_store_ids; empty = all configured stores")
    provider: Optional[str] = Field(None, description="default: PRICE_PROVIDER")
    limit: Optional[int] = Field(None, ge=1)


class V1JobAccepted(BaseModel):
    job_id: int
    status: str
    provider: str


class V1Job(BaseModel):
    id: int
    trigger: str
    provider: str
    status: str
    params: dict
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    products_processed: int = 0
    stores_processed: int = 0
    requests: int = 0
    cached: int = 0
    saved: int = 0
    duplicates: int = 0
    rejected: int = 0
    unavailable: int = 0
    errors: int = 0
    price_changes: int = 0
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _number(n: str) -> str:
    n = n.strip()
    if not n.isdigit() or len(n) not in (9, 10):
        raise HTTPException(400, "internet number must be 9 or 10 digits")
    return n


def _store(db, hd_store_id: str) -> dict:
    sid = hd_store_id.strip().zfill(4)
    st = next((s for s in db.stores() if s["hd_store_id"] == sid), None)
    if not st:
        raise HTTPException(404, f"unknown store {sid}")
    return st


def _price(row: dict, store: Optional[dict] = None) -> dict:
    import json
    try:
        raw = json.loads(row.get("raw") or "{}")
    except ValueError:
        raw = {}
    return {"hd_store_id": (store or row).get("hd_store_id"), "store_name": (store or {}).get("name", row.get("store_name")),
            "price": row.get("price"), "original_price": row.get("original_price"),
            "discount_pct": row.get("discount_pct"), "stock": row.get("stock"), "availability": row.get("availability"),
            "recorded_at": row["scraped_at"], "observed_at": raw.get("observed_at"), "source": raw.get("source"),
            "price_scope": raw.get("price_scope"), "mocked": bool(raw.get("mocked"))}


def _product(p: dict) -> dict:
    return {**p, "active": bool(p.get("active", 1))}


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.get("/health", response_model=V1Health, tags=["v1 system"])
def v1_health():
    """No key needed. `degraded` means the database could not be read."""
    s = settings_mod.settings
    db_ok, last = "ok", None
    try:
        db = _db()
        db._q("SELECT 1")
        from .collector import jobs
        ok = jobs.recent(db, 1, status="succeeded")
        last = ok[0]["finished_at"] if ok else None
    except Exception:
        db_ok = "error"
    return V1Health(status="ok" if db_ok == "ok" else "degraded", version=__version__, database=db_ok,
                    database_mode=s.database_mode, price_provider=os.getenv("PRICE_PROVIDER", "hd_direct"),
                    scrape_enabled=s.scrape_enabled, api_keys_configured=bool(settings_mod.api_keys()),
                    last_successful_collection=last, time=datetime.now(UTC).isoformat(timespec="seconds"))


@secured.get("/products", response_model=list[V1Product], tags=["v1 products"])
def v1_products(q: Optional[str] = Query(None, description="search name / brand / internet number / sku"),
                include_inactive: bool = Query(False, description="also list products no longer tracked"),
                limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    rows = _db().products(active_only=not include_inactive)
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in f"{r.get('name')} {r.get('brand')} {r['internet_number']} {r.get('sku')}".lower()]
    return [_product(r) for r in rows[offset:offset + limit]]


@secured.get("/products/by-sku/{sku}", response_model=V1ProductDetail, tags=["v1 products"])
def v1_product_by_sku(sku: str):
    if not sku.strip().isdigit():
        raise HTTPException(400, "sku must be digits")
    p = _db().product_by_sku(sku)
    if not p:
        raise HTTPException(404, "no product with that sku")
    return v1_product(p["internet_number"])


@secured.get("/products/{internet_number}", response_model=V1ProductDetail, tags=["v1 products"])
def v1_product(internet_number: str):
    db = _db()
    p = db.product_by_internet_number(_number(internet_number))
    if not p:
        raise HTTPException(404, "unknown product")
    latest = []
    for s in db.stores():
        row = db.last_price_row(p["id"], s["id"])
        if row:
            latest.append(_price(row, s))
    return {**_product(p), "latest": latest}


@secured.get("/stores", response_model=list[V1Store], tags=["v1 stores"])
def v1_stores():
    return _db().stores()


@secured.get("/stores/{hd_store_id}", response_model=V1Store, tags=["v1 stores"])
def v1_store(hd_store_id: str):
    return _store(_db(), hd_store_id)


@secured.get("/stores/{hd_store_id}/prices", response_model=list[V1StorePrice], tags=["v1 stores"])
def v1_store_prices(hd_store_id: str, limit: int = Query(500, ge=1, le=5000)):
    """Latest price of every tracked product at this store."""
    db = _db()
    st = _store(db, hd_store_id)
    out = []
    for r in db.latest_prices():
        if r["hd_store_id"] == st["hd_store_id"]:
            out.append({**_price(r, st), "internet_number": r["internet_number"], "name": r.get("name")})
    return out[:limit]


@secured.get("/prices/{internet_number}/history", response_model=V1History, tags=["v1 prices"])
def v1_history(internet_number: str, store: Optional[str] = Query(None, description="hd_store_id; default all"),
               days: int = Query(120, ge=1, le=730)):
    db = _db()
    p = db.product_by_internet_number(_number(internet_number))
    if not p:
        raise HTTPException(404, "unknown product")
    stores = [_store(db, store)] if store else db.stores()
    return {"internet_number": p["internet_number"], "days": days,
            "stores": {s["hd_store_id"]: [_price(r, s) for r in db.history(p["id"], s["id"], days)] for s in stores}}


@secured.post("/observations", response_model=V1ObservationOut, status_code=201, tags=["v1 prices"],
              responses={200: {"description": "identical observation already stored; nothing added"},
                         422: {"description": "observation failed validation"}})
def v1_submit(body: V1ObservationIn, response: Response, key_name: str = Depends(require_key),
              rescore: bool = Query(True, description="re-score and send Telegram alerts like manual entry")):
    """Submit one observation. Saved with source `api:<key name>`, through the same write path
    as manual entry and the collector. Re-sending the same observation adds nothing."""
    from .collector import jobs
    from .collector.validate import validate
    from .providers.base import Observation
    from .providers.save import save_observation
    db = _db()
    source = f"api:{key_name}"
    data = body.model_dump(exclude={"note", "observed_at"})
    try:
        obs = Observation(source=source, **data,
                          **({"observed_at": body.observed_at} if body.observed_at else {}))
    except (TypeError, ValueError) as e:
        raise HTTPException(422, str(e))
    problems = validate(obs, {s["hd_store_id"] for s in db.stores()})
    if problems:
        jobs.record_error(db, None, source, "rejected", obs.internet_number, obs.hd_store_id, "; ".join(problems))
        raise HTTPException(422, problems)
    obs.extra.update({"submitted_by": key_name, "via": "api_v1", "note": body.note})
    res = save_observation(obs, db, details=True)
    if res.created and rescore:
        from . import detect
        detect.run()
    if not res.created:
        response.status_code = 200
    return V1ObservationOut(id=res.row_id, created=res.created, price_changed=res.price_changed,
                            previous_price=res.previous_price, source=source)


def _run_job(job_id: int, body: V1CollectIn, provider) -> None:
    from .collector import run_collection
    from .notify import send
    res = run_collection(internet_numbers=body.internet_numbers, skus=body.skus, stores=body.stores,
                         provider=provider, trigger="api", limit=body.limit, job_id=job_id, notify=send)
    if res.get("saved"):
        from . import detect
        detect.run()


@secured.post("/collect", response_model=V1JobAccepted, status_code=202, tags=["v1 jobs"])
def v1_collect(body: V1CollectIn, bg: BackgroundTasks):
    """Start a collection job in the background. Poll GET /v1/jobs/{job_id} for the result."""
    from .collector import jobs
    from .collector.engine import TargetError, _valid_number, resolve_stores
    from .providers import available, get_provider
    name = (body.provider or os.getenv("PRICE_PROVIDER", "hd_direct")).strip().lower()
    if name not in available():
        raise HTTPException(400, f"unknown provider {name!r}; choose one of {available()}")
    if name == "hd_direct" and not settings_mod.settings.scrape_enabled:
        raise HTTPException(409, "hd_direct is off (SCRAPE_ENABLED=false); choose another provider")
    db = _db()
    try:
        resolve_stores(db, body.stores)
        for n in body.internet_numbers:
            _valid_number(n)
    except TargetError as e:
        raise HTTPException(400, str(e))
    provider = get_provider(name)
    job_id = jobs.create(db, "api", provider.name, body.model_dump())
    bg.add_task(_run_job, job_id, body, provider)
    return V1JobAccepted(job_id=job_id, status="queued", provider=provider.name)


@secured.get("/jobs", response_model=list[V1Job], tags=["v1 jobs"])
def v1_jobs(limit: int = Query(20, ge=1, le=200),
            status: Optional[Literal["queued", "running", "succeeded", "stopped", "failed", "skipped"]] = None):
    from .collector import jobs
    return jobs.recent(_db(), limit, status)


@secured.get("/jobs/{job_id}", response_model=V1Job, tags=["v1 jobs"])
def v1_job(job_id: int):
    from .collector import jobs
    j = jobs.get(_db(), job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    return j


@secured.get("/monitoring", tags=["v1 system"])
def v1_monitoring():
    """Collection status, last successful run, products/stores processed, provider and API
    usage, provider errors and price changes (last 24 h)."""
    from .collector.monitor import snapshot
    return snapshot(_db())


router.include_router(secured)
