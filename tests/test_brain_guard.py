"""Daily guard: GitHub's late schedules still give one run per day in the morning, the run
button never sends a second summary, and the workflow self-tests before running."""
from pathlib import Path

import pytest
import yaml

from brain import guard

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "brain-daily.yml"
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


@pytest.mark.parametrize("hour", [5, 7, 9])
def test_schedule_runs_in_the_morning_when_today_is_missing(tmp_path, hour):
    assert guard.decide("schedule", hour, TODAY, state(tmp_path, "2026-11-20")) == \
        (True, f"scheduled run at {hour}:00", False)


@pytest.mark.parametrize("hour", [0, 2, 4])
def test_schedule_waits_when_it_starts_in_the_night(tmp_path, hour):
    run, reason, late = guard.decide("schedule", hour, TODAY, state(tmp_path, "2026-11-20"))
    assert run is False and late is False and "too early" in reason


@pytest.mark.parametrize("hour", [2, 7, 13, 21])
def test_any_schedule_skips_when_today_already_ran(tmp_path, hour):
    run, reason, late = guard.decide("schedule", hour, TODAY, state(tmp_path, TODAY))
    assert run is False and late is False and "already" in reason


@pytest.mark.parametrize("hour", [10, 13, 16])
def test_late_run_still_runs_and_is_flagged(tmp_path, hour):
    run, _, late = guard.decide("schedule", hour, TODAY, state(tmp_path, "2026-11-20"))
    assert run is True and late is True


def test_manual_run_goes_ahead_when_today_is_missing(tmp_path):
    run, _, late = guard.decide("workflow_dispatch", 8, TODAY, state(tmp_path, "2026-11-20"))
    assert run is True and late is False


def test_manual_run_does_not_send_a_second_summary(tmp_path):
    run, reason, _ = guard.decide("workflow_dispatch", 8, TODAY, state(tmp_path, TODAY))
    assert run is False and "force" in reason


def test_forced_manual_run_goes_ahead(tmp_path):
    run, reason, _ = guard.decide("workflow_dispatch", 8, TODAY, state(tmp_path, TODAY), force=True)
    assert run is True and "forced" in reason


def test_a_typical_late_github_day_sends_exactly_one_summary(tmp_path):
    """The real pattern: every schedule starts 7-10 hours late. Night starts wait, the first
    one after 5 AM runs, everything after it (including the button) stops."""
    st = state(tmp_path, "2026-11-20")
    starts = [("schedule", 4), ("schedule", 6), ("schedule", 8), ("workflow_dispatch", 9),
              ("schedule", 16), ("schedule", 19)]
    runs = []
    for event, hour in starts:
        run, _, _ = guard.decide(event, hour, TODAY, st)
        runs.append(run)
        if run:                                          # the brain saves today's snapshot
            with open(st / "snapshot_runs.csv", "a") as f:
                f.write(f"{TODAY},240\n")
    assert runs == [False, True, False, False, False, False]


# ------------------------------------------------------------------ CLI
def run_cli(monkeypatch, tmp_path, st, *args):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    slack = []
    import brain.notify
    monkeypatch.setattr(brain.notify, "slack_alert", lambda text: slack.append(text) or True)
    assert guard.main(["--today", TODAY, "--state", str(st), *args]) == 0
    return out.read_text(), slack


def test_cli_late_run_writes_output_and_tells_slack(monkeypatch, tmp_path):
    st = state(tmp_path / "s", "2026-11-20")
    out, slack = run_cli(monkeypatch, tmp_path, st, "--event", "schedule", "--hour", "16")
    assert out == "run=true\n"
    assert len(slack) == 1 and "tarde" in slack[0]


def test_cli_skip_is_quiet(monkeypatch, tmp_path):
    st = state(tmp_path / "s", TODAY)
    out, slack = run_cli(monkeypatch, tmp_path, st, "--event", "schedule", "--hour", "16")
    assert out == "run=false\n" and slack == []


def test_cli_morning_run_does_not_ping_slack(monkeypatch, tmp_path):
    st = state(tmp_path / "s", "2026-11-20")
    out, slack = run_cli(monkeypatch, tmp_path, st, "--event", "schedule", "--hour", "7")
    assert out == "run=true\n" and slack == []


@pytest.mark.parametrize("force,expected", [("false", "run=false\n"), ("true", "run=true\n")])
def test_cli_button_respects_force(monkeypatch, tmp_path, force, expected):
    st = state(tmp_path / "s", TODAY)
    out, _ = run_cli(monkeypatch, tmp_path, st, "--event", "workflow_dispatch", "--hour", "9", "--force", force)
    assert out == expected


# ------------------------------------------------------------------ workflow wiring
@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_schedules_cover_a_late_github_day(wf):
    on = wf.get("on", wf.get(True))                  # PyYAML reads the key `on` as True
    crons = [c["cron"] for c in on["schedule"]]
    assert len(crons) >= 6
    # spread over the evening before through the morning (UTC hours), so a 7-10 hour delay
    # still lands one of them in the Florida morning
    hours = sorted(int(c.split()[1]) for c in crons)
    assert hours[0] <= 3 and hours[-1] >= 14
    assert "workflow_dispatch" in on and "force" in on["workflow_dispatch"]["inputs"]


def test_workflow_gates_the_brain_on_the_guard(wf):
    steps = {s.get("id"): s for s in wf["jobs"]["run"]["steps"] if s.get("id")}
    names = [s.get("id") for s in wf["jobs"]["run"]["steps"]]
    assert names.index("guard") < names.index("selftest") < names.index("brain") < names.index("save")
    assert "brain.guard" in steps["guard"]["run"]
    assert "pytest" in steps["selftest"]["run"] and "selftest.log" in steps["selftest"]["run"]
    for sid in ("selftest", "brain", "save", "simulated"):
        assert "steps.guard.outputs.run == 'true'" in steps[sid]["if"], sid
    # the failure alert still fires for anything, including the guard itself
    alert = next(s for s in wf["jobs"]["run"]["steps"] if s.get("name") == "Alert Slack on failure")
    assert alert["if"] == "failure()" and "OUT_GUARD" in alert["env"] and "OUT_SELFTEST" in alert["env"]


def test_checkout_reads_the_branch_tip_not_the_trigger_commit(wf):
    checkout = wf["jobs"]["run"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["ref"] == "${{ github.ref }}"


def test_runs_never_overlap(wf):
    assert wf["concurrency"] == {"group": "brain-daily", "cancel-in-progress": False}


def test_slack_failure_report_names_the_guard_step():
    from brain import alert
    assert ("OUT_GUARD", "Revisar si ya corrió hoy", None) in alert.STEPS
    assert ("OUT_SELFTEST", "Autoprueba antes de correr", "selftest.log") in alert.STEPS
