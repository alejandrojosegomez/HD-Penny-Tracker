"""End-to-end: 30 simulated days -> Telegram scans -> training -> predictions -> pages."""
import random
from datetime import date, timedelta

from brain import brain, community, dashboard


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "DATA", tmp_path)
    monkeypatch.setattr(brain, "MODEL_PATH", tmp_path / "model.joblib")
    monkeypatch.setattr(brain, "REPORT_PATH", tmp_path / "model_report.json")
    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "REPORTS", tmp_path / "reports")
    con = community.connect(tmp_path / "brain.db")
    con.executescript(brain.SCHEMA)
    monkeypatch.setattr(brain, "connect", lambda: con)
    return con


def _simulate(con, days=30, items=60):
    rnd = random.Random(1)
    today = date.today()
    start = today - timedelta(days=days - 1)
    plan = {}
    for i in range(items):
        sku = str(1000000000 + i)
        first = start + timedelta(days=rnd.randint(0, days - 18))
        reaches_fl = i % 2 == 0
        plan[sku] = (first, first + timedelta(days=rnd.randint(3, 10)) if reaches_fl else None)
    for k in range(days):
        d = start + timedelta(days=k)
        for sku, (first, fl_day) in plan.items():
            if first <= d <= first + timedelta(days=14):
                states = "TX,GA" + (",FL" if fl_day and d >= fl_day else "")
                community.add_reports(con, "pennyrecon", [{
                    "sku": sku, "name": f"RYOBI drill {sku}" if int(sku) % 3 else f"LED lamp {sku}",
                    "reported_at": d.isoformat(), "states": states, "retail": 20 + int(sku) % 50,
                    "report_count": 5 + int(sku) % 40}])
        brain.snapshot(con, d.isoformat())
    con.execute("INSERT INTO collect_runs (source, ran_at, status, items) VALUES "
                "('pennyrecon', ?, 'ok', 60)", (today.isoformat(),))
    con.commit()


def test_full_daily_cycle(tmp_path, monkeypatch):
    con = _setup(tmp_path, monkeypatch)
    _simulate(con)

    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    now = int(__import__("time").time())
    updates = {"ok": True, "result": [
        {"update_id": 10, "message": {"chat": {"id": 42}, "date": now, "text": "/penny 1015277793 260"}},
        {"update_id": 11, "message": {"chat": {"id": 42}, "date": now, "text": "/no 1000-000-002"}},
        {"update_id": 12, "message": {"chat": {"id": 42}, "date": now, "text": "/penny"}},          # ignored
        {"update_id": 13, "message": {"chat": {"id": 99}, "date": now, "text": "/penny 1015277793"}},  # stranger
    ]}
    monkeypatch.setattr(brain.requests, "get", lambda *a, **k: _Resp(updates))

    rep = brain.run_daily(con)
    assert rep["new_outcomes"] == ["1015277793=penny", "1000000002=not_penny"]
    assert (tmp_path / "tg_offset.txt").read_text() == "14"
    assert rep["trained"], rep["status"]
    assert rep["predictions"] and all(0 <= p <= 1 for p, _, _ in rep["predictions"])
    store = con.execute("SELECT store_id FROM scan_outcomes WHERE result='penny'").fetchone()[0]
    assert store == "0260"

    dashboard.render(con, {"pennyrecon": 60, "ppm": -1, "_new_rows": 0}, rep)
    dash = (tmp_path / "README.md").read_text(encoding="utf-8")
    day = date.today().isoformat()
    daily = (tmp_path / "reports" / day[:4] / day[5:7] / f"{day}.md").read_text(encoding="utf-8")
    index = (tmp_path / "reports" / "README.md").read_text(encoding="utf-8")
    assert "Entrenada" in dash and "Predicciones" in dash and "xychart-beta" in dash
    assert "Predicciones" in daily and "❌ falló" in daily and "1015277793=penny" in daily
    assert index.count("\n| ") >= 30

    # second run the same day must not duplicate anything
    monkeypatch.setattr(brain.requests, "get", lambda *a, **k: _Resp({"ok": True, "result": []}))
    rep2 = brain.run_daily(con)
    assert rep2["new_outcomes"] == [] and rep2["total_outcomes"] == 2
