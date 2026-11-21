"""Dashboard + JSON API. One HTML page, live updates over Server-Sent Events.

Interactive docs: /docs (Swagger) and /redoc.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import __version__
from .detect import score_history
from .settings import append_watchlist, settings
from .storage import get_storage
from .api_v1 import router as api_v1_router

INDEX = Path(__file__).parent.parent / "web" / "index.html"

app = FastAPI(
    title="HD Penny Tracker",
    version=__version__,
    description="Store-level Home Depot clearance tracker: price history, penny score, alerts.",
    openapi_tags=[
        {"name": "items", "description": "Products on the watchlist and their latest price per store."},
        {"name": "alerts", "description": "Scored alerts created by detect.py."},
        {"name": "jobs", "description": "Trigger scrape / detect / community runs in the background."},
        {"name": "system", "description": "Health, stores, run history, live stream."},
        {"name": "v1 products", "description": "Authenticated API (X-API-Key): products."},
        {"name": "v1 stores", "description": "Authenticated API: stores and their latest prices."},
        {"name": "v1 prices", "description": "Authenticated API: price history and observation submission."},
        {"name": "v1 jobs", "description": "Authenticated API: start collection jobs and read their status."},
        {"name": "v1 system", "description": "Health (no key) and monitoring."},
    ],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # the dashboard may be hosted elsewhere (Vercel); the API is read-mostly
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------
class Store(BaseModel):
    id: int
    hd_store_id: str = Field(examples=["6381"])
    name: str
    city: Optional[str] = None
    zip: Optional[str] = None


class HistoryPoint(BaseModel):
    t: str = Field(description="ISO timestamp of the observation")
    p: float = Field(description="price")


class Item(BaseModel):
    product_id: int
    store_id: int
    internet_number: str
    sku: Optional[str] = None
    name: Optional[str] = None
    brand: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    product_url: Optional[str] = None
    hd_store_id: str
    store_name: str
    price: Optional[float] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    stock: Optional[int] = None
    availability: Optional[str] = None
    scraped_at: str
    score: int = Field(ge=0, le=100)
    reasons: list[str]
    history_points: list[HistoryPoint]


class PriceRow(BaseModel):
    price: Optional[float] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    stock: Optional[int] = None
    availability: Optional[str] = None
    scraped_at: str


class Alert(BaseModel):
    id: int
    product_id: int
    store_id: int
    internet_number: str
    name: Optional[str] = None
    hd_store_id: str
    store_name: str
    score: int
    reasons: list[str]
    message: str
    sent: bool
    created_at: str


class Run(BaseModel):
    id: int
    job: str
    started_at: str
    finished_at: Optional[str] = None
    ok: Optional[bool] = None
    detail: Optional[str] = None


class AddResult(BaseModel):
    internet_number: str
    added: bool = Field(description="False if it was already on the watchlist")


class JobResult(BaseModel):
    job: Literal["scrape", "detect", "community"]
    status: Literal["started", "already_running"]


class PriceIn(BaseModel):
    internet_number: str = Field(examples=["335104182"])
    store: str = Field(examples=["0260"], description="hd_store_id")
    price: float = Field(gt=0, examples=[0.01])
    original_price: Optional[float] = Field(None, gt=0, description="regular price; reuses the last known one when empty")
    qty: Optional[int] = Field(None, ge=0, description="units on the shelf; empty = not checked")
    name: Optional[str] = None


class PriceOut(BaseModel):
    internet_number: str
    name: Optional[str] = None
    hd_store_id: str
    store_name: str
    price: float
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    stock: Optional[int] = None
    availability: Optional[str] = None
    score: int
    reasons: list[str]


class ProductBrief(BaseModel):
    internet_number: str
    name: Optional[str] = None
    last_original_price: Optional[float] = None


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    database_mode: str
    scrape_enabled: bool
    products: int
    stores: int
    last_scrape: Optional[str] = None
    time: str


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _items(store: Optional[str] = None, min_score: int = 0, q: Optional[str] = None) -> list[dict]:
    db = get_storage()
    out = []
    ql = (q or "").lower()
    for r in db.latest_prices():
        if store and r["hd_store_id"] != store:
            continue
        if ql and ql not in f"{r.get('name')} {r.get('brand')} {r['internet_number']} {r.get('sku')}".lower():
            continue
        hist = db.history(r["product_id"], r["store_id"])
        reports = db.recent_reports(r["internet_number"])
        score, reasons = score_history(hist, r.get("category"), reports)
        if score < min_score:
            continue
        r = dict(r)
        r.pop("raw", None)
        r["score"], r["reasons"] = score, reasons
        r["history_points"] = [{"t": h["scraped_at"], "p": h["price"]} for h in hist if h.get("price") is not None]
        out.append(r)
    out.sort(key=lambda x: (-x["score"], -(x.get("discount_pct") or 0)))
    return out


def _valid_number(n: str) -> str:
    if not n.isdigit() or len(n) not in (9, 10):
        raise HTTPException(400, "internet number must be 9 or 10 digits")
    return n


_running: set[str] = set()
_lock = threading.Lock()


def _run_job(job: str) -> None:
    try:
        if job == "scrape":
            from .scraper import scrape_all
            scrape_all(trigger="dashboard")
            from .detect import run
            run()
        elif job == "detect":
            from .detect import run
            run()
        elif job == "community":
            from .community import refresh
            refresh()
    finally:
        with _lock:
            _running.discard(job)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return INDEX.read_text()


@app.get("/api/health", response_model=Health, tags=["system"])
def health():
    db = get_storage()
    runs = [r for r in db.last_runs(50) if r["job"] == "scrape" and r.get("finished_at")]
    return Health(status="ok", version=__version__, database_mode=settings.database_mode,
                  scrape_enabled=settings.scrape_enabled, products=len(db.products()), stores=len(db.stores()),
                  last_scrape=runs[0]["finished_at"] if runs else None,
                  time=datetime.now(timezone.utc).isoformat(timespec="seconds"))


@app.get("/api/monitoring", tags=["system"])
def api_monitoring():
    """Collector status for the local dashboard (same data as the authenticated GET /v1/monitoring)."""
    from .collector.monitor import snapshot
    return snapshot(get_storage())


@app.get("/api/stores", response_model=list[Store], tags=["system"])
def api_stores():
    return get_storage().stores()


@app.get("/api/runs", response_model=list[Run], tags=["system"])
def api_runs(limit: int = Query(20, ge=1, le=200)):
    rows = get_storage().last_runs(limit)
    for r in rows:
        r["ok"] = None if r["ok"] is None else bool(r["ok"])
    return rows


@app.get("/api/items", response_model=list[Item], tags=["items"])
def api_items(
    store: Optional[str] = Query(None, description="hd_store_id, e.g. 6381"),
    min_score: int = Query(0, ge=0, le=100),
    q: Optional[str] = Query(None, description="text search on name / brand / internet number / sku"),
):
    return _items(store, min_score, q)


@app.get("/api/history/{internet_number}", response_model=dict[str, list[PriceRow]], tags=["items"])
def api_history(internet_number: str, days: int = Query(120, ge=1, le=730)):
    db = get_storage()
    p = db.product_by_internet_number(_valid_number(internet_number))
    if not p:
        raise HTTPException(404, "unknown product")
    return {s["hd_store_id"]: db.history(p["id"], s["id"], days) for s in db.stores()}


@app.post("/api/items/{internet_number}", response_model=AddResult, status_code=201, tags=["items"])
def api_add(internet_number: str, category: str = "", note: str = ""):
    n = _valid_number(internet_number)
    added = append_watchlist(n, category, note, "manual")
    get_storage().upsert_product(n, category=category or None, source="manual", active=1)
    return AddResult(internet_number=n, added=added)


@app.delete("/api/items/{internet_number}", response_model=AddResult, tags=["items"])
def api_remove(internet_number: str):
    """Stop tracking an item. History is kept; the product is marked inactive."""
    db = get_storage()
    n = _valid_number(internet_number)
    if not db.product_by_internet_number(n):
        raise HTTPException(404, "unknown product")
    db.upsert_product(n, active=0)
    return AddResult(internet_number=n, added=False)


@app.get("/api/products", response_model=list[ProductBrief], tags=["items"])
def api_products():
    """Active products, for the price form's picker (includes items with no price yet)."""
    from .manual import last_original_price
    return [ProductBrief(internet_number=p["internet_number"], name=p.get("name"),
                         last_original_price=last_original_price(p["id"]))
            for p in get_storage().products()]


@app.post("/api/prices", response_model=PriceOut, status_code=201, tags=["items"])
def api_add_price(body: PriceIn):
    """Save a price you saw in the store or in the Home Depot app, then re-score (may send an alert)."""
    from .manual import ManualPriceError, record_price
    try:
        return record_price(body.internet_number, body.store, body.price, body.original_price, body.qty, body.name)
    except ManualPriceError as e:
        raise HTTPException(400, str(e))


@app.get("/api/alerts", response_model=list[Alert], tags=["alerts"])
def api_alerts(limit: int = Query(100, ge=1, le=1000), min_score: int = Query(0, ge=0, le=100)):
    out = []
    for r in get_storage().alerts(limit):
        if r["score"] < min_score:
            continue
        try:
            r["reasons"] = json.loads(r.get("reasons") or "[]")
        except Exception:
            r["reasons"] = []
        r["sent"] = bool(r["sent"])
        out.append(r)
    return out


@app.post("/api/jobs/{job}", response_model=JobResult, status_code=202, tags=["jobs"])
def api_job(job: Literal["scrape", "detect", "community"], bg: BackgroundTasks):
    """Start a job in the background. `scrape` also runs `detect` when it finishes."""
    if job == "scrape" and not settings.scrape_enabled:
        raise HTTPException(409, "scraping is off (SCRAPE_ENABLED=false); enter prices with POST /api/prices")
    with _lock:
        if job in _running:
            return JobResult(job=job, status="already_running")
        _running.add(job)
    bg.add_task(_run_job, job)
    return JobResult(job=job, status="started")


@app.get("/stream", tags=["system"],
         description="Server-Sent Events. Emits an `items` event with the full item list whenever new prices land.")
async def stream():
    async def gen():
        last = None
        while True:
            rows = get_storage()._q("SELECT MAX(id) AS m FROM price_history")
            cur = rows[0]["m"] if rows else None
            if cur != last:
                last = cur
                yield {"event": "items", "data": json.dumps(_items())}
            await asyncio.sleep(5)
    return EventSourceResponse(gen())


app.include_router(api_v1_router)


def main() -> None:
    import uvicorn
    uvicorn.run("tracker.web:app", host=settings.web_host, port=settings.web_port, reload=False)
