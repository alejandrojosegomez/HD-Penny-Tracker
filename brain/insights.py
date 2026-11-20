"""Signals that work from day one, and the scorecard that says whether picks come true.

spreading()       items not in Florida yet that are being reported in more states fast
                  (works before the network has enough history to train)
log_predictions() stores today's picks (spreading + network) so they can be checked later
evaluate()        14 days after a pick: did that item show up reported in Florida?
weekly()          numbers for the Sunday summary
"""
from __future__ import annotations

from datetime import date, timedelta

from .brain import HORIZON_DAYS, MOMENTUM_DAYS

TOP = 10


def spreading(con, limit: int = TOP, today: str | None = None) -> list[dict]:
    today = today or con.execute("SELECT MAX(snap_date) FROM item_snapshots").fetchone()[0]
    if not today:
        return []
    rows = con.execute(
        """SELECT sku, name, retail, n_states, report_count, states_gain_7d, reports_gain_7d
           FROM item_snapshots
           WHERE snap_date = ? AND has_fl = 0 AND (states_gain_7d > 0 OR reports_gain_7d > 0)
           ORDER BY states_gain_7d DESC, reports_gain_7d DESC, n_states DESC, report_count DESC
           LIMIT ?""", (today, limit)).fetchall()
    return [dict(r) for r in rows]


def log_predictions(con, report: dict, today: str | None = None) -> None:
    today = today or con.execute("SELECT MAX(snap_date) FROM item_snapshots").fetchone()[0]
    if not today:
        return
    picks = [("spreading", i, r["sku"], r["name"], r["states_gain_7d"])
             for i, r in enumerate(report.get("spreading") or [], 1)]
    picks += [("network", i, s, n, p) for i, (p, s, n) in enumerate(report.get("predictions") or [], 1)]
    con.execute("DELETE FROM predictions WHERE pred_date = ?", (today,))   # re-runs replace the day
    con.executemany("INSERT OR REPLACE INTO predictions (pred_date, method, rank, sku, name, score) "
                    "VALUES (?,?,?,?,?,?)", [(today, *p) for p in picks])
    con.commit()


def evaluate(con) -> dict:
    """Per method: how many picks old enough to judge, how many reached Florida, and the
    rate for a random not-yet-in-Florida item over the same days (to compare against)."""
    last = con.execute("SELECT MAX(snap_date) FROM item_snapshots").fetchone()[0]
    if not last:
        return {}
    cutoff = (date.fromisoformat(last) - timedelta(days=HORIZON_DAYS)).isoformat()
    hit_sql = """EXISTS (SELECT 1 FROM item_snapshots f WHERE f.sku = {t}.sku AND f.has_fl = 1
                         AND f.snap_date > {t}.{d} AND f.snap_date <= date({t}.{d}, '+{h} day'))"""
    out = {}
    for method in ("spreading", "network"):
        row = con.execute(
            f"""SELECT COUNT(*) n, SUM({hit_sql.format(t='p', d='pred_date', h=HORIZON_DAYS)}) hits
                FROM predictions p WHERE p.method = ? AND p.pred_date <= ?""",
            (method, cutoff)).fetchone()
        if row["n"]:
            out[method] = {"judged": row["n"], "hits": row["hits"] or 0,
                           "rate": round((row["hits"] or 0) / row["n"], 3)}
    base = con.execute(
        f"""SELECT COUNT(*) n, SUM({hit_sql.format(t='s', d='snap_date', h=HORIZON_DAYS)}) hits
            FROM item_snapshots s WHERE s.has_fl = 0 AND s.snap_date <= ?
              AND s.snap_date IN (SELECT DISTINCT pred_date FROM predictions)""",
        (cutoff,)).fetchone()
    if base["n"]:
        out["random"] = {"judged": base["n"], "hits": base["hits"] or 0,
                         "rate": round((base["hits"] or 0) / base["n"], 3)}
    first = con.execute("SELECT MIN(pred_date) FROM predictions").fetchone()[0]
    out["first_judged_on"] = (date.fromisoformat(first) + timedelta(days=HORIZON_DAYS)).isoformat() \
        if first else None
    return out


def weekly(con, today: str | None = None) -> dict:
    today = today or con.execute("SELECT MAX(snap_date) FROM item_snapshots").fetchone()[0]
    week_ago = (date.fromisoformat(today) - timedelta(days=MOMENTUM_DAYS)).isoformat()
    new = con.execute("SELECT COUNT(*) FROM (SELECT sku, MIN(date(first_collected)) d FROM community_reports GROUP BY sku) "
                      "WHERE d > ?", (week_ago,)).fetchone()[0]
    fl_now = con.execute("SELECT COUNT(*) FROM item_snapshots WHERE snap_date = ? AND has_fl = 1",
                         (today,)).fetchone()[0]
    fl_then = con.execute(
        "SELECT COUNT(*) FROM item_snapshots WHERE has_fl = 1 AND snap_date = "
        "(SELECT MIN(snap_date) FROM item_snapshots WHERE snap_date >= ?)", (week_ago,)).fetchone()[0]
    arrived = con.execute(
        """SELECT DISTINCT a.sku, a.name FROM item_snapshots a
           WHERE a.snap_date > ? AND a.has_fl = 1
             AND EXISTS (SELECT 1 FROM item_snapshots b         -- seen without FL first
                         WHERE b.sku = a.sku AND b.has_fl = 0 AND b.snap_date < a.snap_date)
             AND NOT EXISTS (SELECT 1 FROM item_snapshots b
                         WHERE b.sku = a.sku AND b.has_fl = 1 AND b.snap_date < a.snap_date)
           LIMIT 10""", (week_ago,)).fetchall()
    failed = con.execute("SELECT source, COUNT(*) FROM collect_runs WHERE status = 'error' "
                         "AND date(ran_at) > ? GROUP BY source", (week_ago,)).fetchall()
    return {"new_products": new, "fl_now": fl_now, "fl_week_ago": fl_then,
            "arrived_fl": [dict(r) for r in arrived],
            "source_errors": {r[0]: r[1] for r in failed}}
