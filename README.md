# HD Penny Tracker

Tracks Home Depot clearance prices store by store, keeps the full price history, scores
how close an item is to the $0.01 "penny" stage, and sends me a Telegram alert when
something is worth driving out to scan. Built for my local stores around Melbourne, FL,
but it works for any store number.

> Penny items never show as $0.01 online. The price lives in the store's POS. What this
> tool does is find the items that are *about* to get there (deep markdown, long time on
> shelf, several markdowns in a row, the "ZMA" flip to full price + unavailable) so I know
> where to go scan with the Home Depot app. The scan at self-checkout is the only
> confirmation.

## What it does

```
Watchlist (CSV / CLI) ─┐
Community penny lists ─┼─► worker (every 4h) ─► price_history ─► detect.py (score 0-100)
                       │                                              │
                       │                                              ├─► Telegram alert (score ≥ 70)
                       └──────────────────────────────────────────────┴─► live dashboard (SSE)
```

* **Store-level prices** – pulls price, original price, stock and availability for each
  product in each of my stores. Uses a real browser session once to pass the bot check,
  then reuses the cookies with `curl_cffi` so it is light and polite (one request every
  2–4 s).
* **History, not snapshots** – every run appends to `price_history`. Nothing is
  overwritten, so dwell time and markdown count are real numbers, not guesses.
* **Scoring** – depth of discount, number of markdowns, days at the current price,
  community reports, seasonal timing, and the remote "ZMA" signal (price jumps back to
  full + item goes unavailable in that store). Price endings (.03/.06) count for almost
  nothing on purpose – the data says they are not reliable.
* **Community feed** – pulls internet numbers from public penny lists (PennyCentral,
  PennyRecon, Penny Pinchin' Mom, Paydirt) once a day and checks them against my stores.
* **Alerts** – Telegram message with name, store, price, score and the reasons.
* **Dashboard** – one page, updates live over Server-Sent Events: photo, description,
  price per store, % off, score badge, and a "scan today" list I open on my phone in the
  aisle.
* **Two storage modes** – SQLite by default (zero setup), Supabase/Postgres when
  `DATABASE_MODE=supabase` (same schema, see `supabase/schema.sql`).

## Stack

Python 3.12 · Playwright · curl_cffi · SQLite / Supabase · FastAPI + sse-starlette ·
APScheduler · Telegram Bot API · Docker

## Quick start

```bash
git clone https://github.com/alejandrojosegomez/HD-Penny-Tracker.git
cd HD-Penny-Tracker
cp .env.example .env            # fill in TELEGRAM_* (and SUPABASE_* if you use it)
pip install -r requirements.txt
playwright install chromium

# add a product by its internet number (9 or 10 digits) (the number at the end of the product URL)
python -m tracker add 203401615 --category patio

# one scrape of everything on the watchlist, in every configured store
python -m tracker scrape

# score + send alerts for anything that crossed the threshold
python -m tracker detect

# dashboard on http://localhost:8000
python -m tracker serve
```

Or everything at once with Docker:

```bash
docker compose up -d        # worker (scheduler) + dashboard
```

## Configuration

`config/stores.yaml` – the stores to track. Mine:

| storeId | Store | ZIP |
|---|---|---|
| 0260 | West Melbourne | 32904 |
| 6381 | N Melbourne (Wickham) | 32940 |
| 6336 | Palm Bay | 32907 |
| 0430 | Viera / Rockledge | 32955 |

`config/watchlist.csv` – products to track (internet number, category, note). The CLI
`add` command appends to it; the community job adds rows automatically with
`source=community`.

`.env` – see `.env.example`. Nothing secret is committed.

## How the Home Depot request works

`homedepot.com` sits behind Akamai Bot Manager, so a plain `requests.get` gets a 403.
The worker:

1. Opens a real Chromium once with Playwright, loads a product page with the store
   selected, and exports the cookies + user agent (`tracker/session.py`).
2. Reuses that session with `curl_cffi` (`impersonate="chrome"`) to call the internal
   GraphQL endpoint `federation-gateway/graphql` with `storeId` + `zipCode`
   (`tracker/hd_client.py`).
3. Falls back to parsing the product page HTML (JSON-LD + Apollo state) if the GraphQL
   call is refused.
4. Detects a block (403 / `errors.edgesuite.net`) and refreshes the browser session, with
   backoff. If it keeps failing it stops and tells me on Telegram instead of hammering.

The GraphQL query shipped in `tracker/graphql/product.graphql` is a minimal field set.
Home Depot changes it from time to time; if it breaks, open a product page in Chrome
with DevTools → Network → filter `graphql`, copy the `productClientOnlyProduct` request
and paste the query into that file.

This runs from a residential IP (my own machine / a Raspberry Pi). Datacenter IPs get
blocked fast, and that is fine – this is a personal tool, low volume, no reselling.

## Scoring (tracker/detect.py)

| Signal | Rule | Points |
|---|---|---|
| Discount depth | ≥90% → 30 · ≥95% → 40 · ≥99% → 50 | 30–50 |
| Markdown count | 2nd distinct markdown → 15 · 3rd → 25 | 15–25 |
| Dwell time | same deep price >14 d with stock → 15 · >30 d → 25 | 15–25 |
| ZMA flip | price back to full + unavailable/out of stock, had stock <7 d ago | 40 |
| Community report | internet number on a public penny list in last 14 d + stock here | 35 |
| Inactive | dropped out of category search but still answers by item id | 20 |
| Seasonal | seasonal category 2–6 weeks after a season reset | 10 |
| Price ending | .02 / .03 | 5 |

Score ≥ 70 → Telegram "go scan today". 45–69 → weekly list. Below → history only.

## Project layout

```
tracker/
  settings.py      env + config loading
  storage.py       SQLite / Supabase backends, same interface
  session.py       Playwright bootstrap → cookies for curl_cffi
  hd_client.py     GraphQL + HTML fallback, block detection
  scraper.py       watchlist × stores → price_history
  community.py     public penny lists → community_reports + watchlist
  detect.py        scoring rules → alerts
  notify.py        Telegram
  scheduler.py     APScheduler jobs (scrape 4h, community daily, detect after scrape)
  web.py           FastAPI: /api/items, /api/history, /api/alerts, /stream, dashboard
  cli.py           python -m tracker <command>
  graphql/         query text
web/index.html     the dashboard
supabase/schema.sql
config/            stores.yaml, watchlist.csv
```

## Roadmap

- [x] Store-level scrape with session reuse and block detection
- [x] History + scoring + Telegram
- [x] Live dashboard
- [ ] Category sweep (`searchModel` with `navParam`) to discover clearance I am not watching yet
- [ ] Supabase Realtime + Next.js front end on Vercel
- [ ] Lowe's module (their terms changed in 2026, needs its own review)

## Notes on using it responsibly

Buying a penny item is legal; the store can still refuse and that is their call. This
tool never touches checkout, never creates accounts, never scrapes behind a login, and
keeps request volume to what one person browsing would do. Don't change tags, don't hide
stock, don't argue with the cashier.

## License

MIT – see [LICENSE](LICENSE.md).

---

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/signature-light.png">
    <img src="assets/signature-dark.png" alt="Alejandro Gomez" width="360">
  </picture>
</p>
