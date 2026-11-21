"""Shared fixture for the collector / v1 API tests: a throwaway SQLite db, mock provider,
two API keys and a Telegram stub. Nothing touches data/, the network or the real bot."""
import importlib

import pytest

KEY_BRAIN = "brain-key-0123456789abcdef"
KEY_BOT = "telegram-key-0123456789abcdef"


@pytest.fixture()
def platform(tmp_path, monkeypatch):
    env = {
        "DATABASE_MODE": "sqlite", "SQLITE_PATH": str(tmp_path / "platform.db"),
        "SCRAPE_ENABLED": "false", "PRICE_PROVIDER": "mock",
        "API_KEYS": f"penny-brain:{KEY_BRAIN},telegram-bot:{KEY_BOT}",
        "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_TOKEN": "", "TELEGRAM_CHAT_ID": "",
        "COLLECTOR_MAX_PER_MINUTE": "0", "COLLECTOR_MAX_PER_DAY": "0", "COLLECTOR_CACHE_TTL_MINUTES": "60",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MOCK_PROVIDER_FILE", raising=False)
    import tracker.settings as s; importlib.reload(s)
    import tracker.storage as st; importlib.reload(st)
    import tracker.notify as nt; importlib.reload(nt)
    import tracker.detect as d; importlib.reload(d)
    import tracker.manual as m; importlib.reload(m)
    import tracker.scraper as sc; importlib.reload(sc)
    import tracker.web as w; importlib.reload(w)

    sent: list[str] = []
    fake_send = lambda msg: sent.append(msg) or True          # noqa: E731
    monkeypatch.setattr(nt, "send", fake_send)
    monkeypatch.setattr(d, "send", fake_send)
    monkeypatch.setattr(sc, "send", fake_send)
    monkeypatch.setattr(sc, "read_watchlist", lambda: [])
    monkeypatch.setattr(w, "append_watchlist", lambda n, c="", note="", src="manual": True)

    db = st.get_storage()
    assert db.conn.execute("PRAGMA database_list").fetchone()[2] == str(tmp_path / "platform.db")

    from fastapi.testclient import TestClient

    class P:
        pass
    p = P()
    p.db, p.sent, p.manual, p.scraper, p.detect = db, sent, m, sc, d
    p.client = TestClient(w.app)
    p.brain = {"X-API-Key": KEY_BRAIN}
    p.bot = {"X-API-Key": KEY_BOT}
    return p
