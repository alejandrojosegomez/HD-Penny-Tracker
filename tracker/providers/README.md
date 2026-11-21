# Price providers

Store-level prices reach `price_history` through one path: a provider returns an
`Observation`, the collector (`tracker/collector/`) validates it, and `save.save_observation()`
writes it. Re-sending an identical observation (same source, observed_at and values) adds nothing. Manual entry (`tracker/manual.py`)
builds the same `Observation`, so alerts and scoring don't care where a price came from.

## Observation (base.py)

| Field | Notes |
|---|---|
| `internet_number` | digits only |
| `hd_store_id` | 4 digits, zero-padded; must be in `config/stores.yaml` |
| `price`, `original_price` | rounded to cents, never negative |
| `stock` | units on the shelf, or `None` if unknown |
| `availability` | `in_stock`, `out_of_stock`, `limited` or `unknown` |
| `source` | provider name (`manual`, `sample`, `licensed_api`, `hd_direct`) |
| `observed_at` | ISO time; defaults to now |
| `name`, `sku`, `upc`, `model`, `brand`, `description`, `image_url`, `product_url` | optional product details |
| `extra` | anything else worth keeping; stored in `price_history.raw` |

## Statuses

`ok`, `unavailable` (no data for that product/store), `rate_limited`, `access_denied`,
`malformed` (answer could not be parsed), `error` (network or unexpected).

`rate_limited` and `access_denied` stop the run at once. The scraper sends one Telegram
notice and makes no further requests until the next scheduled run. The other non-ok
statuses are logged and the run moves on.

## Providers

Optional attributes the collector reads: `mocked`, `cache_allowed`, `capabilities`
(`store_level_price`, `clearance_price`, `inventory`, `sku_lookup`; anything missing is False),
`max_per_minute`, `max_per_day`, and `lookup_sku(sku)`. See docs/PLATFORM.md.

- **`mock.py`**: scriptable fake responses (fixtures/mock_responses.json or MOCK_PROVIDER_FILE),
  SKU lookups, per-call sequences and any status. Observations are marked `mocked`.

- **`sample.py`**: reads a JSON list of observations (`tests/fixtures/sample_observations.json`).
  A row may carry `"status": "rate_limited"` (or any status) to simulate a failure.
- **`licensed_api.py`**: integration point for a licensed data API. Before using one,
  confirm in writing that its terms allow store-level Home Depot price and inventory
  lookups for this use, and check its real coverage of your stores. Then implement
  `_request()` and `_normalize()`, put the key in `.env` as `PRICE_API_KEY`, and add a
  test with a saved fake response.
- **`hd_direct.py`**: wraps the original `tracker/hd_client.py`. Home Depot's Akamai Bot
  Manager blocks it; blocks are reported as `access_denied`.

## Out of scope

CAPTCHA solving, stealth or human-behavior emulation, fingerprint spoofing, proxy rotation,
or retrying harder after a refusal. A provider that is refused reports it and stops.

## Adding a provider

1. New file in this folder with a class that has `name`, `fetch(internet_number, store) -> ProviderResult`
   and `pause()`.
2. Register it in `registry.py`.
3. Tests in `tests/test_providers.py` using fixtures, never live requests.
