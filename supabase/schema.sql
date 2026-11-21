-- HD Penny Tracker – Postgres / Supabase schema
-- Paste into the Supabase SQL editor. Same tables the SQLite backend uses.

create table if not exists stores (
  id          serial primary key,
  hd_store_id text unique not null,          -- '0260', '6381', ...
  name        text,
  city        text,
  zip         text
);

create table if not exists products (
  id              serial primary key,
  internet_number text unique not null,      -- 9 or 10 digits, the itemId in the URL / GraphQL
  sku             text,                      -- 6-digit store SKU
  upc             text,                      -- package barcode (what you scan)
  model           text,
  name            text,
  description     text,
  brand           text,
  category        text,
  image_url       text,
  product_url     text,
  source          text default 'manual',     -- manual | community
  active          boolean default true,
  created_at      timestamptz default now()
);

create table if not exists price_history (
  id             bigserial primary key,
  product_id     int not null references products(id) on delete cascade,
  store_id       int not null references stores(id) on delete cascade,
  price          numeric(10,2),
  original_price numeric(10,2),
  discount_pct   numeric(5,2),
  stock          int,
  availability   text,                       -- in_stock | limited | out_of_stock | unavailable | unknown
  scraped_at     timestamptz not null default now()
);
create index if not exists ix_price_history on price_history (product_id, store_id, scraped_at desc);

create table if not exists alerts (
  id         serial primary key,
  product_id int not null references products(id) on delete cascade,
  store_id   int not null references stores(id) on delete cascade,
  score      int not null,
  reasons    jsonb,
  message    text,
  sent       boolean default false,
  created_at timestamptz default now()
);

create table if not exists community_reports (
  id              serial primary key,
  internet_number text not null,
  source          text not null,
  source_url      text,
  reported_at     date not null,
  unique (internet_number, source, reported_at)
);

-- latest observation per product/store (what the dashboard shows)
create or replace view latest_prices as
select distinct on (ph.product_id, ph.store_id)
       ph.*, p.internet_number, p.name, p.brand, p.category, p.image_url, p.product_url,
       s.hd_store_id, s.name as store_name
from price_history ph
join products p on p.id = ph.product_id
join stores   s on s.id = ph.store_id
where p.active
order by ph.product_id, ph.store_id, ph.scraped_at desc;

-- how long each item has been at its current price in each store
create or replace view dwell as
select product_id, store_id, price, min(scraped_at) as since, now() - min(scraped_at) as dwell
from price_history
group by product_id, store_id, price;

-- Realtime: enable on price_history and alerts in the Supabase dashboard
-- (Database -> Replication) so a front end can subscribe to inserts.

-- ---------------------------------------------------------------------------
-- Collector / API bookkeeping. The tracker keeps these in its local SQLite file
-- (data/tracker.db) in both modes; they are here so the Postgres schema matches if you
-- later want to mirror them. Nothing writes them to Supabase today.
-- ---------------------------------------------------------------------------
create table if not exists collection_jobs (
  id                 serial primary key,
  trigger            text not null,          -- schedule | manual | api | dashboard | cli
  provider           text not null,
  status             text not null,          -- queued | running | succeeded | stopped | failed | skipped
  params             jsonb,
  started_at         timestamptz,
  finished_at        timestamptz,
  products_processed int default 0,
  stores_processed   int default 0,
  requests           int default 0,
  cached             int default 0,
  saved              int default 0,
  duplicates         int default 0,
  rejected           int default 0,
  unavailable        int default 0,
  errors             int default 0,
  price_changes      int default 0,
  detail             text,
  created_at         timestamptz not null default now()
);

create table if not exists provider_errors (
  id              bigserial primary key,
  job_id          int,
  provider        text not null,
  status          text not null,
  internet_number text,
  hd_store_id     text,
  detail          text,
  created_at      timestamptz not null default now()
);

create table if not exists provider_requests (
  id       bigserial primary key,
  provider text not null,
  at       timestamptz not null
);
create index if not exists ix_provider_requests on provider_requests (provider, at);

create table if not exists provider_cache (
  provider        text not null,
  internet_number text not null,
  hd_store_id     text not null,
  payload         jsonb not null,
  fetched_at      timestamptz not null,
  primary key (provider, internet_number, hd_store_id)
);

create table if not exists api_usage (
  key_name  text not null,
  day       date not null,
  requests  int not null default 0,
  last_path text,
  last_at   timestamptz,
  primary key (key_name, day)
);
