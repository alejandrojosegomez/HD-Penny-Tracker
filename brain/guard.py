"""Decides whether the daily workflow should run the brain today.

GitHub starts scheduled workflows on a best-effort basis. For this repo every scheduled run
has been starting 7-10 hours late (the 7:00 AM run fired around 4-5 PM), and on 2026-11-21
one never fired at all. So the workflow has several schedules spread through the night and
morning (UTC), and every run asks this guard first:

  * scheduled run, today's snapshot already saved           -> skip (no second summary)
  * scheduled run before EARLIEST_HOUR (Florida time)        -> skip (no summary at 2 AM)
  * scheduled run otherwise                                  -> run
  * manual run ("Run workflow" button), today already saved  -> skip unless force=true
  * manual run otherwise                                     -> run

Whichever schedule GitHub starts first after EARLIEST_HOUR does the day's work; the rest
find today's snapshot and stop. If the run only happens at LATE_HOUR or later, Slack gets
a short note so a late summary is never a mystery.

    python -m brain.guard --event schedule
    python -m brain.guard --event workflow_dispatch --force true
"""
from __future__ import annotations

import argparse
import csv
import os
from datetime import date, datetime
from pathlib import Path

STATE = Path(__file__).parent / "state"
EARLIEST_HOUR = 5    # Florida time: scheduled runs before this wait for a later schedule
LATE_HOUR = 10       # Florida time: a run starting this late is reported to Slack


def ran_today(today: str | None = None, state: Path = STATE) -> bool:
    """True if today's snapshot is already saved (the run that produced it sent the summary)."""
    today = today or date.today().isoformat()
    path = state / "snapshot_runs.csv"
    if not path.exists():
        return False
    with open(path, newline="", encoding="utf-8") as f:
        return any((r.get("snap_date") or "").strip() == today for r in csv.DictReader(f))


def decide(event: str, hour: int, today: str | None = None, state: Path = STATE,
           force: bool = False) -> tuple[bool, str, bool]:
    """Returns (run, reason, late). `hour` is the local (Florida) hour the run started."""
    today = today or date.today().isoformat()
    done = ran_today(today, state)
    if event != "schedule":
        if done and not force:
            return False, (f"today's run already happened ({today}); a manual run would send a second "
                           f"summary. Use force=true to run anyway"), False
        return True, f"manual run ({event}{', forced' if force else ''})", False
    if done:
        return False, f"today's run already happened ({today}); nothing to do", False
    if hour < EARLIEST_HOUR:
        return False, f"too early ({hour}:00 < {EARLIEST_HOUR}:00); a later schedule will run it", False
    return True, f"scheduled run at {hour}:00", hour >= LATE_HOUR


def _bool(s: str | None) -> bool:
    return (s or "").strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--event", required=True, help="github.event_name")
    ap.add_argument("--force", default="false", help="manual runs only: run even if today already ran")
    ap.add_argument("--hour", type=int, default=None, help="local hour (default: now)")
    ap.add_argument("--today", default=None)
    ap.add_argument("--state", type=Path, default=STATE, help="folder with snapshot_runs.csv")
    ap.add_argument("--no-slack", action="store_true")
    a = ap.parse_args(argv)

    hour = datetime.now().hour if a.hour is None else a.hour
    run, reason, late = decide(a.event, hour, a.today, a.state, _bool(a.force))
    print(f"guard: {'RUN' if run else 'SKIP'} - {reason}")
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={'true' if run else 'false'}\n")
    if late and not a.no_slack:
        from .notify import slack_alert
        slack_alert(f":warning: *Penny Brain*: GitHub arrancó la corrida diaria tarde ({hour}:00). "
                    "Se está haciendo ahora; el resumen de Telegram llega en un minuto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
