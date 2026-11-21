# HD Penny Intelligence Platform

The collection engine and the authenticated API that sit on top of HD Penny Tracker. They
extend the tracker; they don't replace it. The dashboard, manual price entry, scoring,
Telegram alerts and the Penny Brain work as before.

```
 providers (adapters)          collector                    storage                 readers
 ─────────────────────         ──────────────────           ─────────────           ────────────────────
 mock      (fake data)  ─┐     products × stores            price_history  ───────▶ dashboard  /api/*
 sample    (fixtures)   ─┼──▶  budget → cache → fetch  ──▶  products               /v1 API (X-API-Key)
 licensed_api (stub)    ─┤     → validate → save            collection_jobs        Telegram (detect.py)
 hd_direct (blocked)    ─┘     (one write path)             provider_errors…       Penny Brain (own data)
                                     ▲
 manual entry / POST /v1/observations┘  (same save_observation, same validation rules)
```

> **Live data status:** no live data source is connected. `mock` and `sample` return made-up
> data (stored with `"mocked": true`); `licensed_api` is an empty integration point that refuses
> every request until a vendor is configured; `hd_direct` is blocked by Home Depot. Real prices
> today come only from manual entry. See [Connecting a live source](#connecting-a-permitted-live-source).

---

## 1. What's where

| Path | What it does |
|---|---|
| `tracker/collector/engine.py` | `run_collection()`: resolves targets (all / products / SKUs / stores), runs the provider, validates, saves, records the job. The old `scraper.scrape_all()` now calls it. |
| `tracker/collector/validate.py` | Rules every observation must pass (also used by `POST /v1/observations`). |
| `tracker/collector/limits.py` | Request budget per provider, per minute and per UTC day, stored in the database. |
| `tracker/collector/cache.py` | Short-lived answer cache, **only** for providers that declare `cache_allowed = True`. |
| `tracker/collector/jobs.py` | `collection_jobs` and `provider_errors` tables. |
| `tracker/collector/monitor.py` | The monitoring snapshot used by the dashboard and `/v1/monitoring`. |
| `tracker/providers/` | Adapter interface (`base.py`), registry, and the adapters: `mock`, `sample`, `licensed_api`, `hd_direct`. |
| `tracker/providers/save.py` | `save_observation()`: the **only** function that writes price rows. Ignores exact re-sends. |
| `tracker/api_v1.py` | The `/v1` API (mounted on the existing FastAPI app in `tracker/web.py`). |
| `web/index.html` | Dashboard; new **📈 Monitoreo** view under *Sistema*. |

### New database tables (additive, created automatically)

`collection_jobs`, `provider_errors`, `provider_requests` (request budget), `provider_cache`,
`api_usage`. Existing tables are unchanged. In Supabase mode these stay in the local SQLite
mirror; `supabase/schema.sql` has matching definitions if you want them in Postgres later.

---

## 2. Configuration (`.env`)

Copy `.env.example` to `.env`. The new settings:

| Variable | Default | Meaning |
|---|---|---|
| `API_KEYS` | *(empty)* | `name:key,name2:key2`. Keys ≥ 16 characters. Empty = `/v1` answers 503 (except `/v1/health`). The name becomes the `source` of what that key submits. |
| `PRICE_PROVIDER` | `hd_direct` | Default provider: `mock`, `sample`, `licensed_api`, `hd_direct`. |
| `COLLECTOR_MAX_PER_MINUTE` | `30` | Requests per minute per provider. When full, the collector **waits**. `0` = none. |
| `COLLECTOR_MAX_PER_DAY` | `2000` | Requests per UTC day per provider. When used up, the job **stops** and a Telegram notice is sent. |
| `COLLECTOR_CACHE_TTL_MINUTES` | `60` | Reuse a cached answer this long (only providers that allow caching). `0` = off. |
| `COLLECTOR_MAX_PRICE` | `50000` | Prices above this are rejected. |
| `COLLECTOR_MAX_AGE_DAYS` | `30` | Observations older than this are rejected. |
| `MOCK_PROVIDER_FILE` | built-in fixture | JSON responses for the `mock` provider. |
| `PRICE_API_KEY`, `PRICE_API_URL` | *(empty)* | `licensed_api` credentials. |
| `PRICE_API_MAX_PER_MINUTE`, `PRICE_API_MAX_PER_DAY` | `0` | The vendor plan's limits. The stricter of these and `COLLECTOR_*` wins. |
| `PRICE_API_CAPABILITIES` | *(empty)* | What the vendor was **verified** to return: `store_level_price,clearance_price,inventory,sku_lookup`. |
| `PRICE_API_CACHE_ALLOWED` | `false` | `true` only if the vendor's terms allow caching. |

`SCRAPE_ENABLED=false` (the current mode) keeps the **schedule** off and blocks `hd_direct`
everywhere. You can still run jobs by hand with `mock`, `sample` or a configured `licensed_api`.

---

## 3. Collector

### Running jobs

```bash
python -m tracker collect                                  # every active product × every store
python -m tracker collect --product 1006423739             # one product, all stores
python -m tracker collect --sku 1001234567 --store 0260    # by store SKU, one store
python -m tracker collect --provider mock --no-detect      # pick the provider; skip re-scoring
python -m tracker jobs                                     # recent jobs and their counts
```

From the API: `POST /v1/collect` (see below). On a schedule: the worker (`python -m tracker run`)
runs the full collection every `SCRAPE_EVERY_HOURS` between 6:00 and 22:00 when
`SCRAPE_ENABLED=true`. One collection runs at a time across containers (file lock in `data/`);
a second one is recorded as `skipped`.

### What happens to each product × store

1. **Cache**: if the provider allows caching and a fresh answer is cached, it's reused (no request).
2. **Budget**: otherwise one request is reserved. Per-minute full → wait. Per-day used up → job stops (`provider_errors.status = budget`).
3. **Fetch**: the provider returns a status: `ok`, `unavailable`, `malformed`, `error`, `rate_limited`, `access_denied`.
4. **Refusals stop the job**: `rate_limited` / `access_denied` end the job immediately, with no retries and one Telegram notice.
5. **Validate**: store is configured; internet # is 9–10 digits; SKU is digits; `original_price ≥ price`; prices ≤ `COLLECTOR_MAX_PRICE`; `observed_at` is a valid ISO time, not in the future, not older than `COLLECTOR_MAX_AGE_DAYS`. Failures are counted as `rejected` and logged in `provider_errors`.
6. **Attribute and save** through `save_observation()`. `price_history.raw` gets `source`, `observed_at`, `provider`, `job_id`, `price_scope` (`store` if the provider verified store-level prices, else `unverified`), `inventory_verified`, and `mocked: true` for fake data.
7. **No duplicates**: an observation with the same product, store, source, `observed_at` and values as a stored one is not written again (counted as `duplicates`).
8. **Price changes**: a saved price that differs from the previous row for that product/store is counted.

Every request is logged (`data/tracker.log` and container logs) as
`job=… provider=… item=… store=… status=… cached=… ms=…`.

### Job statuses

`queued` → `running` → `succeeded` | `stopped` (refusal or budget) | `failed` (bad target or crash) | `skipped` (another collection was running).

---

## 4. Adapter interface (adding a data source)

A provider is any class with:

```python
class MyProvider:
    name = "my_source"                    # stored as the observation source
    mocked = False                        # True only for fake data
    cache_allowed = False                 # True only if the source's terms allow it
    capabilities = {}                     # only what you verified: store_level_price, clearance_price,
                                          # inventory, sku_lookup (missing = False)
    max_per_minute = 0                    # the source's own limits (0 = none)
    max_per_day = 0

    def fetch(self, internet_number: str, store: dict) -> ProviderResult: ...
    def pause(self) -> None: ...          # wait between requests if the source requires it
    def lookup_sku(self, sku: str) -> str | None: ...   # optional; used only with sku_lookup
```

`fetch` returns `ProviderResult.ok(Observation(...))` or a `ProviderResult(Status.X, detail=...)`.
Register it in `tracker/providers/registry.py`, add fixture-based tests, and select it with
`PRICE_PROVIDER` or `--provider`. Nothing else in the collector, API or dashboard changes.

---

## 5. API reference (`/v1`)

Interactive docs with "Try it out": **`http://localhost:8000/docs`** (Swagger) and `/redoc`.
Click *Authorize* and paste a key.

**Auth:** header `X-API-Key: <key>`. Missing or wrong key → `401`; no keys configured → `503`.
Every authenticated request is counted per key per day (`api_usage`, shown in monitoring).

| Method | Path | Description |
|---|---|---|
| GET | `/v1/health` | **No key.** `status` (`ok`/`degraded`), database, provider, last successful collection. |
| GET | `/v1/products?q=&include_inactive=&limit=&offset=` | Products (tracked by default). |
| GET | `/v1/products/{internet_number}` | One product + its latest price at each store. |
| GET | `/v1/products/by-sku/{sku}` | Same, looked up by store SKU. |
| GET | `/v1/stores` | Configured stores. |
| GET | `/v1/stores/{hd_store_id}` | One store (`260` and `0260` both work). |
| GET | `/v1/stores/{hd_store_id}/prices` | Latest price of every tracked product at that store. |
| GET | `/v1/prices/{internet_number}/history?store=&days=120` | Price history, per store, oldest first. |
| POST | `/v1/observations?rescore=true` | Submit one observation (below). |
| POST | `/v1/collect` | Start a collection job (below). `202` with `job_id`. |
| GET | `/v1/jobs?limit=&status=` | Recent jobs. |
| GET | `/v1/jobs/{job_id}` | One job with its counts. |
| GET | `/v1/monitoring` | Collection status, usage, errors, price changes (same as the dashboard view). |

Every price row returned has: `price`, `original_price`, `discount_pct`, `stock`, `availability`,
`recorded_at` (stored), `observed_at` (seen), `source`, `price_scope`, `mocked`.

### Submit an observation

```http
POST /v1/observations
X-API-Key: <key>
Content-Type: application/json

{"internet_number": "1006423739", "hd_store_id": "0260", "price": 0.03, "original_price": 129.0,
 "stock": 2, "availability": "limited", "observed_at": "2026-11-20T14:05:00", "note": "shelf tag"}
```

* `201` → saved: `{"id": 42, "created": true, "price_changed": true, "previous_price": 0.06, "source": "api:<key name>"}`
* `200` → the identical observation was already stored; nothing added (`created: false`).
* `422` → failed validation; `detail` lists the reasons (also logged in monitoring).
* With `rescore=true` (default) the item is re-scored right away and a Telegram alert is sent at 70+, the same as manual entry.

### Start a job

```http
POST /v1/collect
{"internet_numbers": ["1006423739"], "skus": [], "stores": ["0260", "6381"], "provider": "mock"}
```

All fields are optional (empty = everything with `PRICE_PROVIDER`). `400` for an unknown
provider, store or malformed internet number; `409` for `hd_direct` while `SCRAPE_ENABLED=false`.
Then poll `GET /v1/jobs/{job_id}`.

---

## 6. Setup on Windows (PowerShell)

Prerequisites: [Docker Desktop](https://www.docker.com/products/docker-desktop/) running, and Git.

```powershell
git clone https://github.com/alejandrojosegomez/HD-Penny-Tracker.git
```

```powershell
cd HD-Penny-Tracker
```

```powershell
git checkout feature/hd-penny-intelligence-platform
```

```powershell
Copy-Item .env.example .env
```

Generate an API key (run once per key; copy the output):

```powershell
-join ((48..57) + (97..122) | Get-Random -Count 40 | ForEach-Object {[char]$_})
```

Open `.env` and set at least `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and, for example,
`API_KEYS=penny-brain:<key1>,telegram-bot:<key2>`. To try the platform with fake data, also set
`PRICE_PROVIDER=mock`.

```powershell
notepad .env
```

Build and start both services:

```powershell
docker compose up -d --build
```

```powershell
docker compose ps
```

Open the dashboard at http://localhost:8000 (📈 Monitoreo is in the sidebar) and the API docs at
http://localhost:8000/docs. Check the API:

```powershell
Invoke-RestMethod http://localhost:8000/v1/health
```

```powershell
$h = @{ "X-API-Key" = "<key1>" }
```

```powershell
Invoke-RestMethod http://localhost:8000/v1/stores -Headers $h
```

Run a collection job with the mock provider and read the result:

```powershell
docker compose exec worker python -m tracker collect --provider mock
```

```powershell
Invoke-RestMethod http://localhost:8000/v1/jobs -Headers $h
```

Submit an observation:

```powershell
$body = @{ internet_number = "1006423739"; hd_store_id = "0260"; price = 0.03; original_price = 129.0; stock = 2 } | ConvertTo-Json
```

```powershell
Invoke-RestMethod http://localhost:8000/v1/observations -Method Post -Headers $h -ContentType "application/json" -Body $body
```

Logs, restart and stop:

```powershell
docker compose logs -f worker
```

```powershell
docker compose restart
```

```powershell
docker compose down
```

> Mock data lands in your real `data/tracker.db`, marked `source: mock` and `mocked: true`.
> To try it without touching your real data, set `SQLITE_PATH=data/demo.db` in `.env` while testing.

### Without Docker (PowerShell)

```powershell
py -3.12 -m venv .venv
```

```powershell
.\.venv\Scripts\Activate.ps1
```

```powershell
pip install -r requirements-dev.txt
```

```powershell
python -m pytest -q
```

```powershell
python -m tracker serve
```

---

## 7. Deployment

* **Where:** the same machine as today (your PC or a Raspberry Pi). Both services run from one image; `data/` holds the database, log and lock.
* **Network:** the port is published on `127.0.0.1` only. To let another device or app reach `/v1`, change the port line in `docker-compose.yml` to `"8000:8000"`. The dashboard and `/api/*` have **no login**, so only do that on a network you trust. For access from outside your home, put it behind a reverse proxy with HTTPS (e.g. Caddy or Cloudflare Tunnel). Never expose plain HTTP with API keys to the internet.
* **Secrets:** API keys, the Telegram token and vendor keys live only in `.env`, which is git-ignored and not copied into the image (`.dockerignore`).
* **Linux / Raspberry Pi hosts:** the containers run as the non-root user `pwuser` (uid 1000), so `data/` must be writable by it, or both containers exit with `PermissionError: /app/data/tracker.log`. Run `sudo chown -R 1000:1000 data` once. Docker Desktop on Windows/macOS doesn't need this.
* **Updating:** `git pull`, then `docker compose up -d --build`. Tables are created automatically; nothing needs migrating.

## 8. Maintenance

* **Rotate an API key:** edit `API_KEYS` in `.env`, then `docker compose restart web`. Old keys stop working immediately.
* **Watch:** the 📈 Monitoreo view, or `GET /v1/monitoring`. A `stopped` job means a refusal or the daily budget; the reason is in the job's `detail` and in `provider_errors`.
* **Backups:** copy `data/tracker.db` (stop the containers first, or use `sqlite3 data/tracker.db ".backup backup.db"`).
* **Growth:** `provider_requests` prunes itself to 2 days. `provider_errors`, `collection_jobs` and `api_usage` grow slowly (one row per job / error / key-day).
* **Tests:** `python -m pytest -q` runs everything with temporary databases and mocked providers. GitHub runs the tracker suite on every push to `tracker/`, `web/`, `config/` or the tests.

---

## Connecting a permitted live source

What is actually needed before any live data flows. None of this is done yet.

1. **A source that permits this use.** A data vendor (or Home Depot itself, through an official program) whose written terms allow retrieving Home Depot product prices, and ideally store-level prices and inventory, for your use. Scraping homedepot.com is not such a source: it is blocked by Akamai, and this project won't get around that.
2. **Verify coverage before coding.** Ask the vendor for sample responses for your four store IDs (`0260`, `6381`, `6336`, `0430`) and a few known items. Check whether prices are **per store** or national, whether **clearance/markdown** prices appear at all (penny prices usually live only in store registers), whether inventory is per store, the update frequency, and SKU vs internet-number lookup.
3. **Plan limits and caching terms:** requests per minute/day, cost per request, whether caching responses is allowed and for how long.
4. **Implement the adapter:** fill in `_request()` and `_normalize()` in `tracker/providers/licensed_api.py` (or a new file), using the vendor's documented API only.
5. **Configure:** `PRICE_API_KEY`, `PRICE_API_URL`, `PRICE_API_MAX_PER_MINUTE`, `PRICE_API_MAX_PER_DAY`, `PRICE_API_CACHE_ALLOWED`, and `PRICE_API_CAPABILITIES` listing **only** what step 2 verified.
6. **Test with recorded responses:** add a fixture of real (anonymised) vendor responses and tests for mapping, errors (401/403/429/404/5xx) and validation. No live calls in tests.
7. **Trial run:** `python -m tracker collect --provider licensed_api --product <one item> --store 0260`, compare against the shelf or the Home Depot app, check `GET /v1/jobs/{id}` and `price_scope` on the saved rows.
8. **Then** set `PRICE_PROVIDER=licensed_api` and `SCRAPE_ENABLED=true` to put it on the schedule.

Until step 7 has been done against the real source, live retrieval should be treated as **not working**.
