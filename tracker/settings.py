"""Environment + config loading. Everything the other modules need comes from here."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Store:
    store_id: str
    name: str
    city: str = ""
    zip: str = ""


@dataclass
class Settings:
    database_mode: str = _env("DATABASE_MODE", "sqlite").lower()
    sqlite_path: Path = ROOT / _env("SQLITE_PATH", "data/tracker.db")
    supabase_url: str = _env("SUPABASE_URL")
    supabase_key: str = _env("SUPABASE_SERVICE_KEY")

    # TELEGRAM_TOKEN is the name the brain uses; accept either so one .env works for both
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN") or _env("TELEGRAM_TOKEN")
    telegram_chat_id: str = _env("TELEGRAM_CHAT_ID")

    delay_min: float = _env_float("REQUEST_DELAY_MIN", 2.0)
    delay_max: float = _env_float("REQUEST_DELAY_MAX", 4.0)
    headless: bool = _env("HEADLESS", "true").lower() != "false"
    # false = Home Depot is blocking the scraper: no automatic scrapes, prices are entered by hand
    scrape_enabled: bool = _env("SCRAPE_ENABLED", "true").lower() != "false"
    scrape_every_hours: int = _env_int("SCRAPE_EVERY_HOURS", 4)
    community_hour: int = _env_int("COMMUNITY_HOUR", 7)
    community_max: int = _env_int("COMMUNITY_MAX", 60)
    timezone: str = _env("TIMEZONE", "America/New_York")

    alert_threshold: int = _env_int("ALERT_THRESHOLD", 70)
    watch_threshold: int = _env_int("WATCH_THRESHOLD", 45)

    web_host: str = _env("WEB_HOST", "0.0.0.0")
    web_port: int = _env_int("WEB_PORT", 8000)

    session_file: Path = DATA_DIR / "session.json"
    stores: list[Store] = field(default_factory=list)
    seasonal_categories: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        cfg = yaml.safe_load((CONFIG_DIR / "stores.yaml").read_text()) or {}
        self.stores = [Store(**s) for s in cfg.get("stores", [])]
        self.seasonal_categories = [c.lower() for c in cfg.get("seasonal_categories", [])]


settings = Settings()


def api_keys() -> dict[str, str]:
    """API_KEYS=name1:key1,name2:key2 -> {key: name}. Read on every call so a .env change
    applies after a restart and tests can set it. Keys shorter than 16 characters are ignored."""
    out: dict[str, str] = {}
    for part in os.getenv("API_KEYS", "").split(","):
        name, sep, key = part.strip().partition(":")
        if sep and name.strip() and len(key.strip()) >= 16:
            out[key.strip()] = name.strip()
    return out

WATCHLIST_PATH = CONFIG_DIR / "watchlist.csv"
WATCHLIST_FIELDS = ["internet_number", "category", "note", "source"]


def read_watchlist() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    with WATCHLIST_PATH.open(newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("internet_number")]
    # de-dupe, keep first occurrence
    seen, out = set(), []
    for r in rows:
        key = r["internet_number"].strip()
        if key and key not in seen:
            seen.add(key)
            r["internet_number"] = key
            out.append(r)
    return out


def append_watchlist(internet_number: str, category: str = "", note: str = "", source: str = "manual") -> bool:
    """Append a row if the internet number is not already there. Returns True if added."""
    existing = {r["internet_number"] for r in read_watchlist()}
    internet_number = internet_number.strip()
    if internet_number in existing:
        return False
    new_file = not WATCHLIST_PATH.exists() or WATCHLIST_PATH.stat().st_size == 0
    with WATCHLIST_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=WATCHLIST_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({"internet_number": internet_number, "category": category, "note": note, "source": source})
    return True
