"""Decides whether the daily workflow should run the brain, so a missed schedule is
recovered without ever sending two Telegram summaries on the same day.

GitHub starts scheduled workflows on a best-effort basis: they are often late and, under
load, a scheduled run is sometimes skipped entirely (it happened on 2026-11-21). The
workflow therefore has a main schedule plus backup schedules. Each scheduled run checks
whether today's run already happened (today's row in brain/state/snapshot_runs.csv, which
every successful run commits) and only goes ahead if it didn't. A backup that has to step
in posts a note to Slack so a missed main run is never silent.

Manual runs ("Run workflow" button) always go ahead: running by hand is a deliberate choice.

    python -m brain.guard --event schedule --schedule "17 14 * * *" --primary "0 12 * * *"
"""
from __future__ import annotations

import argparse
import csv
import os
from datetime import date
from pathlib import Path

STATE = Path(__file__).parent / "state"


def ran_today(today: str | None = None, state: Path = STATE) -> bool:
    """True if today's snapshot is already saved (the run that produced it sent the summary)."""
    today = today or date.today().isoformat()
    path = state / "snapshot_runs.csv"
    if not path.exists():
        return False
    with open(path, newline="", encoding="utf-8") as f:
        return any((r.get("snap_date") or "").strip() == today for r in csv.DictReader(f))


def decide(event: str, schedule: str, primary: str, today: str | None = None,
           state: Path = STATE) -> tuple[bool, str, bool]:
    """Returns (run, reason, is_backup)."""
    today = today or date.today().isoformat()
    if event != "schedule":
        return True, f"manual run ({event})", False
    if ran_today(today, state):
        return False, f"today's run already happened ({today}); nothing to do", False
    if schedule.strip() == primary.strip():
        return True, "main schedule", False
    return True, f"backup schedule ({schedule}): the main run for {today} did not happen", True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--event", required=True, help="github.event_name")
    ap.add_argument("--schedule", default="", help="github.event.schedule (the cron that fired)")
    ap.add_argument("--primary", required=True, help="the main cron")
    ap.add_argument("--today", default=None)
    ap.add_argument("--state", type=Path, default=STATE, help="folder with snapshot_runs.csv")
    ap.add_argument("--no-slack", action="store_true")
    a = ap.parse_args(argv)

    run, reason, backup = decide(a.event, a.schedule, a.primary, a.today, a.state)
    print(f"guard: {'RUN' if run else 'SKIP'} - {reason}")
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={'true' if run else 'false'}\n")
    if backup and not a.no_slack:
        from .notify import slack_alert
        slack_alert(":warning: *Penny Brain*: la corrida de las 7:00 AM no se hizo (GitHub no la arrancó "
                    "a tiempo). El horario de respaldo la está haciendo ahora; el resumen de Telegram "
                    "llega en un minuto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
