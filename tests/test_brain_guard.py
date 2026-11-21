"""Backup schedule guard: a missed scheduled run is recovered, and there is never a second
Telegram summary on the same day."""
from pathlib import Path

import pytest
import yaml

from brain import guard

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "brain-daily.yml"
PRIMARY, BACKUP1, BACKUP2 = "0 12 * * *", "17 14 * * *", "43 17 * * *"
TODAY = "2026-11-21"


def state(tmp_path, *days):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "snapshot_runs.csv").write_text("snap_date,items\n" + "".join(f"{d},237\n" for d in days))
    return tmp_path


# ------------------------------------------------------------------ decisions
def test_ran_today(tmp_path):
    assert guard.ran_today(TODAY, state(tmp_path, "2026-11-20", TODAY))
    assert not guard.ran_today(TODAY, state(tmp_path, "2026-11-20"))
    assert not guard.ran_today(TODAY, tmp_path / "missing")


def test_main_schedule_runs_when_today_is_missing(tmp_path):
    assert guard.decide("schedule", PRIMARY, PRIMARY, TODAY, state(tmp_path, "2026-11-20")) == \
        (True, "main schedule", False)


@pytest.mark.parametrize("cron", [PRIMARY, BACKUP1, BACKUP2])
def test_any_schedule_skips_when_today_already_ran(tmp_path, cron):
    run, reason, backup = guard.decide("schedule", cron, PRIMARY, TODAY, state(tmp_path, TODAY))
    assert run is False and backup is False and "already" in reason


@pytest.mark.parametrize("cron", [BACKUP1, BACKUP2])
def test_backup_steps_in_when_main_run_was_missed(tmp_path, cron):
    run, reason, backup = guard.decide("schedule", cron, PRIMARY, TODAY, state(tmp_path, "2026-11-20"))
    assert run is True and backup is True and "backup" in reason


def test_manual_run_always_goes_ahead(tmp_path):
    run, _, backup = guard.decide("workflow_dispatch", "", PRIMARY, TODAY, state(tmp_path, TODAY))
    assert run is True and backup is False


def test_whole_day_sends_exactly_one_summary(tmp_path):
    """Main run skipped by GitHub, first backup recovers, second backup does nothing."""
    st = state(tmp_path, "2026-11-20")
    runs = []
    for cron in (BACKUP1, BACKUP2):                      # the 12:00 run never fired
        run, _, _ = guard.decide("schedule", cron, PRIMARY, TODAY, st)
        runs.append(run)
        if run:                                          # the brain saves today's snapshot
            with open(st / "snapshot_runs.csv", "a") as f:
                f.write(f"{TODAY},240\n")
    assert runs == [True, False]


# ------------------------------------------------------------------ CLI
def run_cli(monkeypatch, tmp_path, st, *args):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    slack = []
    import brain.notify
    monkeypatch.setattr(brain.notify, "slack_alert", lambda text: slack.append(text) or True)
    assert guard.main(["--today", TODAY, "--primary", PRIMARY, "--state", str(st), *args]) == 0
    return out.read_text(), slack


def test_cli_backup_writes_output_and_tells_slack(monkeypatch, tmp_path):
    st = state(tmp_path / "s", "2026-11-20")
    out, slack = run_cli(monkeypatch, tmp_path, st, "--event", "schedule", "--schedule", BACKUP1)
    assert out == "run=true\n"
    assert len(slack) == 1 and "respaldo" in slack[0]


def test_cli_skip_is_quiet(monkeypatch, tmp_path):
    st = state(tmp_path / "s", TODAY)
    out, slack = run_cli(monkeypatch, tmp_path, st, "--event", "schedule", "--schedule", BACKUP2)
    assert out == "run=false\n" and slack == []


def test_cli_main_run_does_not_ping_slack(monkeypatch, tmp_path):
    st = state(tmp_path / "s", "2026-11-20")
    out, slack = run_cli(monkeypatch, tmp_path, st, "--event", "schedule", "--schedule", PRIMARY)
    assert out == "run=true\n" and slack == []


# ------------------------------------------------------------------ workflow wiring
@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_has_main_and_backup_schedules(wf):
    on = wf.get("on", wf.get(True))                  # PyYAML reads the key `on` as True
    crons = [c["cron"] for c in on["schedule"]]
    assert crons == [PRIMARY, BACKUP1, BACKUP2]
    assert wf["jobs"]["run"]["env"]["PRIMARY_CRON"] == crons[0]
    assert "workflow_dispatch" in on


def test_workflow_gates_the_brain_on_the_guard(wf):
    steps = {s.get("id"): s for s in wf["jobs"]["run"]["steps"] if s.get("id")}
    names = [s.get("id") for s in wf["jobs"]["run"]["steps"]]
    assert names.index("guard") < names.index("brain") < names.index("save")
    assert "brain.guard" in steps["guard"]["run"]
    for sid in ("brain", "save", "simulated"):
        assert "steps.guard.outputs.run == 'true'" in steps[sid]["if"], sid
    # the failure alert still fires for anything, including the guard itself
    alert = next(s for s in wf["jobs"]["run"]["steps"] if s.get("name") == "Alert Slack on failure")
    assert alert["if"] == "failure()" and "OUT_GUARD" in alert["env"]


def test_checkout_reads_the_branch_tip_not_the_trigger_commit(wf):
    checkout = wf["jobs"]["run"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["ref"] == "${{ github.ref }}"


def test_runs_never_overlap(wf):
    assert wf["concurrency"] == {"group": "brain-daily", "cancel-in-progress": False}


def test_slack_failure_report_names_the_guard_step():
    from brain import alert
    assert ("OUT_GUARD", "Revisar si ya corrió hoy", None) in alert.STEPS
