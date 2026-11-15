"""Dashboard + JSON API. One HTML page, live updates over Server-Sent Events."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from .detect import score_history
from .settings import append_watchlist, settings
from .storage import get_storage

app = FastAPI(title="HD Penny Tracker")
INDEX = Path(__file__).parent.parent / "web" / "index.html"


def _items() -> list[dict]:
    db = get_storage()
    rows = db.latest_prices()
    out = []
    for r in rows:
        hist = db.history(r["product_id"], r["store_id"])
        reports = db.recent_reports(r["internet_number"])
        score, reasons = score_history(hist, r.get("category"), reports)
        r = dict(r)
        r.pop("raw", None)
        r["score"] = score
        r["reasons"] = reasons
        r["history_points"] = [{"t": h["scraped_at"], "p": h["price"]} for h in hist if h.get("price") is not None]
        out.append(r)
    out.sort(key=lambda x: (-x["score"], -(x.get("discount_pct") or 0)))
    return out


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX.read_text()


@app.get("/api/items")
def api_items():
    return JSONResponse(_items())


@app.get("/api/stores")
def api_stores():
    return JSONResponse(get_storage().stores())


@app.get("/api/alerts")
def api_alerts():
    rows = get_storage().alerts()
    for r in rows:
        try:
            r["reasons"] = json.loads(r.get("reasons") or "[]")
        except Exception:
            r["reasons"] = []
    return JSONResponse(rows)


@app.get("/api/runs")
def api_runs():
    return JSONResponse(get_storage().last_runs())


@app.get("/api/history/{internet_number}")
def api_history(internet_number: str):
    db = get_storage()
    p = db.product_by_internet_number(internet_number)
    if not p:
        raise HTTPException(404, "unknown product")
    return JSONResponse({s["hd_store_id"]: db.history(p["id"], s["id"]) for s in db.stores()})


@app.post("/api/items/{internet_number}")
def api_add(internet_number: str, category: str = "", note: str = ""):
    if not internet_number.isdigit() or len(internet_number) not in (9, 10):
        raise HTTPException(400, "internet number must be 9 or 10 digits")
    added = append_watchlist(internet_number, category, note, "manual")
    get_storage().upsert_product(internet_number, category=category or None, source="manual")
    return {"added": added, "internet_number": internet_number}


@app.get("/stream")
async def stream():
    """Pushes the full item list whenever the newest price_history id changes."""
    async def gen():
        last = None
        while True:
            db = get_storage()
            rows = db._q("SELECT MAX(id) AS m FROM price_history")
            cur = rows[0]["m"] if rows else None
            if cur != last:
                last = cur
                yield {"event": "items", "data": json.dumps(_items())}
            await asyncio.sleep(5)
    return EventSourceResponse(gen())


def main() -> None:
    import uvicorn
    uvicorn.run("tracker.web:app", host=settings.web_host, port=settings.web_port, reload=False)
