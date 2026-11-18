"""Spreading picks, the accuracy scorecard and the CSV state round-trip."""
from datetime import date, timedelta

from brain import brain, community, insights, store

D0 = date(2026, 10, 1)


def _con(tmp_path):
    con = community.connect(tmp_path / "t.db")
    con.executescript(brain.SCHEMA)
    return con


def _day(con, k, items):
    d = (D0 + timedelta(days=k)).isoformat()
    community.add_reports(con, "pennyrecon", [{"sku": s, "name": f"item {s}", "reported_at": d,
                                               "states": st, "report_count": rc} for s, st, rc in items])
    brain.snapshot(con, d)
    return d


def test_spreading_and_accuracy(tmp_path):
    con = _con(tmp_path)
    _day(con, 0, [("A", "TX", 1), ("B", "TX", 1), ("C", "TX", 1)])
    d1 = _day(con, 3, [("A", "TX,GA,NC", 5), ("B", "TX,GA", 2), ("C", "TX", 1)])
    picks = insights.spreading(con)
    assert [p["sku"] for p in picks] == ["A", "B"]           # C isn't moving
    assert picks[0]["states_gain_7d"] == 2
    insights.log_predictions(con, {"spreading": picks})
    # A reaches Florida 5 days later, B never does
    _day(con, 8, [("A", "TX,GA,NC,FL", 9), ("B", "TX,GA", 2), ("C", "TX", 1)])
    acc = insights.evaluate(con)
    assert "spreading" not in acc                             # not 14 days old yet
    assert acc["first_judged_on"] == (date.fromisoformat(d1) + timedelta(days=14)).isoformat()
    _day(con, 20, [("C", "TX", 1)])
    acc = insights.evaluate(con)
    assert acc["spreading"] == {"judged": 2, "hits": 1, "rate": 0.5}
    assert acc["random"]["judged"] == 3 and acc["random"]["hits"] == 1


def test_no_look_ahead_when_backfilling(tmp_path):
    con = _con(tmp_path)
    _day(con, 0, [("A", "TX", 1)])
    _day(con, 5, [("A", "TX,FL", 3)])
    brain.snapshot(con, D0.isoformat())                       # re-snapshot day 0 later
    row = con.execute("SELECT has_fl FROM item_snapshots WHERE sku='A' AND snap_date=?",
                      (D0.isoformat(),)).fetchone()
    assert row["has_fl"] == 0


def test_csv_state_round_trip(tmp_path):
    con = _con(tmp_path)
    _day(con, 0, [("A", "TX", 1), ("B", "", 2)])
    store.save(con, tmp_path / "state")
    con2 = community.connect(tmp_path / "fresh.db")
    con2.executescript(brain.SCHEMA)
    n = store.load(con2, tmp_path / "state")
    assert n > 0
    for t in ("community_reports", "item_snapshots", "snapshot_runs"):
        def rows(c):   # CSV can't tell '' from NULL; both mean "no value" to the brain
            return [tuple(None if v == "" else v for v in r)
                    for r in c.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
        assert rows(con) == rows(con2)
    assert store.load(con2, tmp_path / "state") == 0         # never loads twice
