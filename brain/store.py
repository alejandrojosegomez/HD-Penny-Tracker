"""Brain state as plain CSV files in brain/state/ (one per table), instead of a binary
SQLite file committed every day.

Why: git stores a full copy of a binary file on every commit, so a daily brain.db makes
the repo grow by megabytes a month. CSV rows are text, so each day's commit only adds the
new lines, and you can open the data straight from GitHub or Excel.

How: every run loads the CSVs into a fresh SQLite database (brain/state/brain.db, which is
git-ignored), works on it as before, and writes the CSVs back at the end.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

STATE = Path(__file__).parent / "state"
TABLES = ["community_reports", "collect_runs", "item_snapshots", "snapshot_runs",
          "scan_outcomes", "predictions"]
ORDER = {  # stable row order keeps git diffs small
    "community_reports": "id", "collect_runs": "id", "scan_outcomes": "id",
    "item_snapshots": "snap_date, sku", "snapshot_runs": "snap_date",
    "predictions": "pred_date, method, rank",
}


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def load(con: sqlite3.Connection, state: Path = STATE) -> int:
    """Fills empty tables from their CSVs. Returns rows loaded."""
    n = 0
    for t in TABLES:
        path = state / f"{t}.csv"
        if not path.exists() or con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]:
            continue
        cols = set(_columns(con, t))
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            use = [c for c in (reader.fieldnames or []) if c in cols]   # tolerate old/new columns
            if not use:
                continue
            sql = f"INSERT OR IGNORE INTO {t} ({', '.join(use)}) VALUES ({', '.join('?' * len(use))})"
            rows = [[(r[c] if r[c] != "" else None) for c in use] for r in reader]
        con.executemany(sql, rows)
        n += len(rows)
    con.commit()
    return n


def save(con: sqlite3.Connection, state: Path = STATE) -> None:
    state.mkdir(parents=True, exist_ok=True)
    for t in TABLES:
        cols = _columns(con, t)
        if not cols:
            continue
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {t} ORDER BY {ORDER[t]}").fetchall()
        tmp = state / f"{t}.csv.tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(cols)
            w.writerows([["" if v is None else v for v in r] for r in rows])
        tmp.replace(state / f"{t}.csv")
