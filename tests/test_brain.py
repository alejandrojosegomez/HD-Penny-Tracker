import random
from datetime import date, timedelta

from brain import community
from brain import brain as learn


def _con(tmp_path, monkeypatch):
    monkeypatch.setattr(learn, "MODEL_PATH", tmp_path / "m.joblib")
    monkeypatch.setattr(learn, "REPORT_PATH", tmp_path / "r.json")
    con = community.connect(tmp_path / "t.db")
    con.executescript(learn.SCHEMA)
    return con


def test_waits_for_data(tmp_path, monkeypatch):
    con = _con(tmp_path, monkeypatch)
    rep = learn.train(con)
    assert not rep["trained"] and "collecting data" in rep["status"]


def test_snapshot_and_labels(tmp_path, monkeypatch):
    con = _con(tmp_path, monkeypatch)
    d0 = date(2026, 10, 1)
    community.add_reports(con, "pennyrecon", [{"sku": "1", "reported_at": d0.isoformat(), "states": "TX"}])
    learn.snapshot(con, d0.isoformat())
    d1 = d0 + timedelta(days=5)
    community.add_reports(con, "pennyrecon", [{"sku": "1", "reported_at": d1.isoformat(), "states": "TX,FL"}])
    learn.snapshot(con, d1.isoformat())
    learn.snapshot(con, (d0 + timedelta(days=20)).isoformat())   # extend observed window
    X, y, _ = learn.build_dataset(con)
    assert y == [1]                     # day-0 TX-only snapshot reached FL by day 5


def test_unchanged_report_is_not_rewritten(tmp_path, monkeypatch):
    con = _con(tmp_path, monkeypatch)
    item = {"sku": "1", "reported_at": "2026-10-01", "states": "TX", "report_count": 3}
    assert community.add_reports(con, "pennyrecon", [item]) == 1
    con.execute("UPDATE community_reports SET last_collected = 'old'")
    assert community.add_reports(con, "pennyrecon", [item]) == 0          # same data: untouched
    assert con.execute("SELECT last_collected FROM community_reports").fetchone()[0] == "old"
    assert community.add_reports(con, "pennyrecon", [{**item, "states": "TX,FL"}]) == 1
    row = con.execute("SELECT states, last_collected FROM community_reports").fetchone()
    assert row[0] == "TX,FL" and row[1] != "old"


def test_trains_neural_net_on_synthetic_history(tmp_path, monkeypatch):
    con = _con(tmp_path, monkeypatch)
    random.seed(1)
    start = date(2026, 9, 1)
    for i in range(120):
        popular = i % 2 == 0                       # popular items spread to FL
        reports = random.randint(30, 200) if popular else random.randint(1, 4)
        for day in range(0, 30, 3):
            d = start + timedelta(days=day)
            fl = popular and day >= 9
            con.execute("INSERT INTO item_snapshots (sku, snap_date, name, report_count, n_states, "
                        "has_fl, n_sources, receipt, retail, days_listed) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (str(i), d.isoformat(), "LED light" if popular else "bag", reports,
                         8 if popular else 1, int(fl), 2 if popular else 1, 0, 20.0, day))
    con.commit()
    rep = learn.train(con)
    assert rep["trained"] and rep["cv_auc"] > 0.8
    assert learn.MODEL_PATH.exists()
