"""Self-feeding neural network: "will this penny item reach Florida in the next 14 days?"

Every daily run:
  1. snapshot()  - saves one row per item with today's community signals
  2. read_telegram_outcomes() - stores your in-store scan results (/penny SKU, /no SKU)
  3. train()     - rebuilds the dataset from all snapshots and retrains the network
                   (only once there is enough labeled history; until then it says so)
  4. predict()   - ranks today's non-Florida items by probability of reaching FL

Labels come for free: a snapshot from day D is labeled 1 if the same item shows
up reported in FL within D+1..D+14, else 0. So the model needs ~3 weeks of daily
snapshots before its first training.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from . import community

DATA = Path(__file__).parent / "state"
MODEL_PATH = DATA / "model.joblib"
REPORT_PATH = DATA / "model_report.json"
HORIZON_DAYS = 14
MIN_LABELED = 60
MIN_PER_CLASS = 10

KEYWORDS = {
    "kw_light": r"light|lamp|led|sconce|bulb|chandelier",
    "kw_floor": r"flooring|plank|tile|vinyl",
    "kw_bath": r"toilet|vanity|faucet|shower|sink|bath",
    "kw_tool": r"drill|saw|wrench|ryobi|milwaukee|dewalt|ridgid|tool|blade|bit",
    "kw_outdoor": r"patio|outdoor|grill|garden|lawn|torch|citronella|gazebo|fan",
    "kw_storage": r"storage|bag|tote|rack|shelf|bin|trash",
    "kw_electric": r"outlet|usb|surge|switch|wire|doorbell",
}
FEATURES = ["report_count", "n_states", "n_sources", "receipt", "retail_log",
            "days_listed", *KEYWORDS]

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_snapshots (
  sku TEXT NOT NULL, snap_date TEXT NOT NULL, name TEXT,
  report_count INTEGER, n_states INTEGER, has_fl INTEGER, n_sources INTEGER,
  receipt INTEGER, retail REAL, days_listed INTEGER,
  PRIMARY KEY (sku, snap_date)
);
CREATE TABLE IF NOT EXISTS snapshot_runs (snap_date TEXT PRIMARY KEY, items INTEGER);
CREATE TABLE IF NOT EXISTS scan_outcomes (
  id INTEGER PRIMARY KEY, sku TEXT NOT NULL, store_id TEXT,
  result TEXT NOT NULL,            -- 'penny' | 'not_penny'
  noted_at TEXT NOT NULL, source TEXT DEFAULT 'telegram'
);
"""


def connect():
    con = community.connect()
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------ 1. snapshot ---
def snapshot(con, today: str | None = None) -> int:
    today = today or date.today().isoformat()
    rows = con.execute(
        """SELECT sku, MAX(name) name, COALESCE(MAX(report_count),0) rc,
                  GROUP_CONCAT(COALESCE(states,''), ',') st, COUNT(DISTINCT source) ns,
                  MAX(receipt_confirmed) rcpt, MAX(retail) retail, MIN(reported_at) first_seen
           FROM community_reports
           WHERE date(reported_at) >= date(?, '-14 day')
           GROUP BY sku""", (today,)).fetchall()
    for r in rows:
        states = {s for s in (r["st"] or "").split(",") if s}
        days = (date.fromisoformat(today) - date.fromisoformat(r["first_seen"][:10])).days
        con.execute(
            """INSERT OR REPLACE INTO item_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (r["sku"], today, r["name"], r["rc"], len(states), int("FL" in states),
             r["ns"], r["rcpt"] or 0, r["retail"], max(days, 0)))
    con.execute("INSERT OR REPLACE INTO snapshot_runs VALUES (?,?)", (today, len(rows)))
    con.commit()
    return len(rows)


# ------------------------------------------------- 2. your scan results ---
def read_telegram_outcomes(con) -> list[str]:
    """Reads messages like '/penny 1015277793 0260' or '/no 1015277793' sent to the bot."""
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return []
    off_file = DATA / "tg_offset.txt"
    offset = int(off_file.read_text()) if off_file.exists() else 0
    try:
        upd = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                           params={"offset": offset, "timeout": 0}, timeout=20).json()
    except requests.RequestException:
        return []
    saved = []
    for u in upd.get("result", []):
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat):
            continue
        m = re.match(r"/?(penny|si|yes|no|nope)\s+([\d-]{6,13})(?:\s+(\d{3,4}))?",
                     (msg.get("text") or "").strip(), re.I)
        if not m:
            continue
        result = "penny" if m[1].lower() in ("penny", "si", "yes") else "not_penny"
        sku = community.digits(m[2])
        con.execute("INSERT INTO scan_outcomes (sku, store_id, result, noted_at) VALUES (?,?,?,?)",
                    (sku, (m[3] or "").zfill(4) or None, result,
                     datetime.fromtimestamp(msg.get("date", 0)).isoformat()))
        saved.append(f"{sku}={result}")
    off_file.parent.mkdir(exist_ok=True)
    off_file.write_text(str(offset))
    con.commit()
    return saved


# ---------------------------------------------------------- 3. training ---
def _features(row) -> list[float]:
    import math
    name = (row["name"] or "").lower()
    base = [row["report_count"] or 0, row["n_states"] or 0, row["n_sources"] or 0,
            row["receipt"] or 0, math.log1p(row["retail"] or 0), row["days_listed"] or 0]
    return base + [int(bool(re.search(p, name))) for p in KEYWORDS.values()]


def build_dataset(con):
    # last day we observed (even if no items were active that day)
    last = con.execute("SELECT MAX(d) FROM (SELECT MAX(snap_date) d FROM snapshot_runs "
                       "UNION ALL SELECT MAX(snap_date) FROM item_snapshots)").fetchone()[0]
    if not last:
        return [], [], None
    cutoff = (date.fromisoformat(last) - timedelta(days=HORIZON_DAYS)).isoformat()
    rows = con.execute(
        """SELECT s.*, EXISTS (
               SELECT 1 FROM item_snapshots f WHERE f.sku = s.sku AND f.has_fl = 1
                 AND f.snap_date > s.snap_date
                 AND f.snap_date <= date(s.snap_date, ?)) AS label
           FROM item_snapshots s
           WHERE s.has_fl = 0 AND s.snap_date <= ?""",
        (f"+{HORIZON_DAYS} day", cutoff)).fetchall()
    return [_features(r) for r in rows], [r["label"] for r in rows], last


def train(con) -> dict:
    X, y, last = build_dataset(con)
    days = con.execute("SELECT COUNT(*) FROM snapshot_runs").fetchone()[0] or         con.execute("SELECT COUNT(DISTINCT snap_date) FROM item_snapshots").fetchone()[0]
    pos = sum(y)
    report = {"trained": False, "snapshot_days": days, "labeled": len(y), "positives": pos,
              "updated": datetime.now().isoformat(timespec="seconds")}
    if len(y) < MIN_LABELED or pos < MIN_PER_CLASS or len(y) - pos < MIN_PER_CLASS:
        report["status"] = (f"collecting data: {days} days of snapshots, {len(y)} labeled examples "
                            f"({pos} reached FL). Needs >= {HORIZON_DAYS + 1} days and "
                            f">= {MIN_PER_CLASS} of each class.")
    else:
        import joblib
        from sklearn.model_selection import cross_val_score
        from sklearn.neural_network import MLPClassifier
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        model = make_pipeline(StandardScaler(),
                              MLPClassifier(hidden_layer_sizes=(32, 16), alpha=1e-3,
                                            max_iter=2000,
                                            random_state=0))
        folds = min(5, pos, len(y) - pos)
        auc = cross_val_score(model, X, y, cv=folds, scoring="roc_auc").mean()
        model.fit(X, y)
        joblib.dump(model, MODEL_PATH)
        report.update(trained=True, cv_auc=round(float(auc), 3),
                      status=f"trained neural net on {len(y)} examples, AUC {auc:.2f} "
                             f"(0.5 = coin flip, 1.0 = perfect)")
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    return report


# -------------------------------------------------------- 4. prediction ---
def predict(con, limit: int = 8) -> list[tuple[float, str, str]]:
    if not MODEL_PATH.exists():
        return []
    import joblib
    model = joblib.load(MODEL_PATH)
    today = con.execute("SELECT MAX(snap_date) FROM item_snapshots").fetchone()[0]
    rows = con.execute("SELECT * FROM item_snapshots WHERE snap_date = ? AND has_fl = 0",
                       (today,)).fetchall()
    if not rows:
        return []
    probs = model.predict_proba([_features(r) for r in rows])[:, 1]
    ranked = sorted(zip(probs, rows), key=lambda t: -t[0])[:limit]
    return [(float(p), r["sku"], r["name"] or "") for p, r in ranked]


def run_daily(con=None) -> dict:
    con = con or connect()
    n = snapshot(con)
    outcomes = read_telegram_outcomes(con)
    report = train(con)
    report["snapshot_items"] = n
    report["new_outcomes"] = outcomes
    report["total_outcomes"] = con.execute("SELECT COUNT(*) FROM scan_outcomes").fetchone()[0]
    report["predictions"] = predict(con)
    return report


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(json.dumps(run_daily(), indent=2, default=str))
