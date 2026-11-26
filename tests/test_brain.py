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


def test_known_sku_from_new_source_is_not_new(tmp_path, monkeypatch):
    from brain import dashboard
    con = _con(tmp_path, monkeypatch)
    community.add_reports(con, "pennyrecon", [{"sku": "1", "reported_at": "2026-10-01", "states": "TX"}])
    con.execute("UPDATE community_reports SET first_collected = '2026-10-01T07:00:00'")
    community.add_reports(con, "paydirt", [{"sku": "1", "reported_at": "2026-10-02", "states": "GA"}])
    community.add_reports(con, "paydirt", [{"sku": "2", "reported_at": "2026-10-02", "states": "GA"}])
    con.execute("UPDATE community_reports SET first_collected = '2026-10-02T07:00:00' "
                "WHERE source = 'paydirt'")
    con.executescript("INSERT INTO snapshot_runs (snap_date, items) VALUES ('2026-10-02', 2);")
    day = dashboard.history(con)[-1]
    assert (day["total"], day["new"]) == (2, 1)    # sku 1 was already known; only sku 2 is new


def test_readiness_counts_items_not_rows():
    # one item seen on 30 days that reached FL is one example, not 30
    groups = ["a"] * 30 + [str(i) for i in range(70)]
    y = [1] * 30 + [0] * 70
    r = learn.readiness(20, y, groups)
    assert r["waiting_for"] == "examples" and r["positive_items"] == 1 and r["negative_items"] == 70
    groups2 = [str(i) for i in range(12)] + [f"n{i}" for i in range(60)]
    r2 = learn.readiness(20, [1] * 12 + [0] * 60, groups2)
    assert r2["waiting_for"] is None and r2["positive_items"] == 12
    assert learn.readiness(5, [], [])["waiting_for"] == "days"


def test_waits_for_examples_after_enough_days(tmp_path, monkeypatch):
    """15+ days of history but only a few items reached Florida: no training yet, and the
    report says what it is waiting for instead of looking stuck at 15/15."""
    con = _con(tmp_path, monkeypatch)
    start = date(2026, 10, 1)
    for day in range(16):
        d = (start + timedelta(days=day)).isoformat()
        con.execute("INSERT INTO snapshot_runs VALUES (?, 100)", (d,))
        for i in range(80):
            fl = i < 3 and day >= 5                   # only 3 items ever reach FL
            con.execute("INSERT INTO item_snapshots (sku, snap_date, name, report_count, n_states, "
                        "has_fl, n_sources, receipt, retail, days_listed) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (str(i), d, "bag", 2, 1, int(fl), 1, 0, 20.0, day))
    con.commit()
    rep = learn.train(con)
    assert not rep["trained"] and rep["waiting_for"] == "examples"
    assert rep["positive_items"] == 3 and "3 of 5" in rep["status"]


def test_training_cv_keeps_each_item_in_one_fold(tmp_path, monkeypatch):
    """AUC is measured with items grouped by SKU, so an item never sits in both the
    training and the test fold."""
    from sklearn.model_selection import StratifiedGroupKFold
    seen = {}

    real = StratifiedGroupKFold.split

    def spy(self, X, y=None, groups=None):
        for tr, te in real(self, X, y, groups):
            assert not ({groups[i] for i in tr} & {groups[i] for i in te})
            seen["ok"] = True
            yield tr, te
    monkeypatch.setattr(StratifiedGroupKFold, "split", spy)
    test_trains_neural_net_on_synthetic_history(tmp_path, monkeypatch)
    assert seen.get("ok")


def test_trains_on_day_15_with_few_examples_but_hides_a_coin_flip_model(tmp_path, monkeypatch):
    """The real situation on day 15: ~7 items reached Florida. The network trains, but if its
    AUC is no better than chance its picks are not shown (they're still logged and scored)."""
    from brain import run
    con = _con(tmp_path, monkeypatch)
    start = date(2026, 10, 1)
    random.seed(3)
    for day in range(16):
        d = (start + timedelta(days=day)).isoformat()
        con.execute("INSERT INTO snapshot_runs VALUES (?, 100)", (d,))
        for i in range(120):
            fl = i < 7 and day >= 5                    # 7 items reach FL, unrelated to features
            con.execute("INSERT INTO item_snapshots (sku, snap_date, name, report_count, n_states, "
                        "has_fl, n_sources, receipt, retail, days_listed) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (str(i), d, "bag", random.randint(1, 5), random.randint(1, 3), int(fl), 1, 0,
                         20.0, day))
    con.commit()
    rep = learn.train(con)
    assert rep["trained"] and rep["positive_items"] == 7
    rep["predictions"] = [(0.9, "1", "bag")]
    rep["reliable"] = False
    text = run.caption(con, {"pennyrecon": 1}, rep)
    assert "aún no supera al azar" in text and "🔮" not in text
    rep["reliable"] = True
    assert "🔮" in run.caption(con, {"pennyrecon": 1}, rep)
