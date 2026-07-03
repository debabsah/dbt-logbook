import stat
from datetime import datetime, timezone

import pytest

from dbt_logbook import serve as serve_mod
from dbt_logbook.serve import (
    NotifyConfig,
    Runner,
    Schedule,
    alert_decision,
    build_alert_text,
    due_schedules,
    load_config,
)
from dbt_logbook.paths import store_path
from dbt_logbook.store import open_store


def make_project(tmp_path, config: str | None = None):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: p\nversion: '1.0'\n")
    if config is not None:
        (project / "dbt-logbook.yml").write_text(config)
    return project


# ---------- config ----------

def test_load_config_missing_file_is_empty(tmp_path):
    project = make_project(tmp_path)
    schedules, notify = load_config(project)
    assert schedules == []
    assert notify.on == ["failure", "recovery"]


def test_load_config_full(tmp_path):
    project = make_project(tmp_path, """
schedules:
  hourly:
    cron: "0 * * * *"
    command: dbt build --select state:modified
    retries: 2
    env: prod
notify:
  slack_webhook: https://hooks.slack.example/x
  on: [failure]
""")
    schedules, notify = load_config(project)
    assert len(schedules) == 1
    s = schedules[0]
    assert s.command == ["dbt", "build", "--select", "state:modified"]
    assert s.retries == 2 and s.env == "prod"
    assert notify.slack_webhook and notify.on == ["failure"]


def test_load_config_rejects_bad_cron(tmp_path):
    project = make_project(tmp_path, 'schedules:\n  x:\n    cron: "not cron"\n    command: dbt build\n')
    with pytest.raises(ValueError, match="invalid cron"):
        load_config(project)


# ---------- transitions ----------

@pytest.mark.parametrize(
    "prev,new,on,expected",
    [
        (None, "error", ["failure", "recovery"], "failure"),
        ("success", "error", ["failure", "recovery"], "failure"),
        ("error", "success", ["failure", "recovery"], "recovery"),
        ("success", "success", ["failure", "recovery"], None),
        ("error", "error", ["failure"], "failure"),  # still failing -> still alert
        ("error", "success", ["failure"], None),      # recovery not subscribed
    ],
)
def test_alert_decision(prev, new, on, expected):
    assert alert_decision(prev, new, on) == expected


def test_alert_text_mentions_schedule_and_run():
    t = build_alert_text("failure", "hourly", "abc123", "exit 2")
    assert "hourly" in t and "abc123" in t and "failed" in t


# ---------- scheduling ----------

def test_due_schedules_fires_and_advances():
    s = Schedule(name="x", cron="*/5 * * * *", command=["true"])
    t0 = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    assert due_schedules([s], t0) == []          # initializes next_fire
    assert s.next_fire is not None
    later = s.next_fire
    assert due_schedules([s], later) == [s]      # due exactly at fire time
    assert s.next_fire > later                   # advanced


# ---------- runner: retries + alerting ----------

def make_fake_dbt(project, fail_times: int):
    """Fails `fail_times` times (tracked in a counter file), then succeeds.
    Writes fresh artifacts each attempt."""
    import json
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures" / "dbt-1.11"
    staged = project / "staged"
    staged.mkdir()
    for name in ("manifest.json", "run_results.json"):
        doc = json.loads((fixtures / name).read_text())
        doc["metadata"]["generated_at"] = "2999-01-01T00:00:00Z"
        (staged / name).write_text(json.dumps(doc))
    script = project / "fake_dbt.sh"
    script.write_text(f"""#!/bin/sh
count_file={project}/attempts
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n+1)); echo $n > "$count_file"
mkdir -p {project}/target
sed "s/\\"invocation_id\\": \\"[^\\"]*\\"/\\"invocation_id\\": \\"attempt-$n\\"/" {staged}/run_results.json > {project}/target/run_results.json
cp {staged}/manifest.json {project}/target/manifest.json
[ $n -le {fail_times} ] && exit 2 || exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_runner_retries_then_succeeds_no_failure_alert(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    script = make_fake_dbt(project, fail_times=1)
    posted = []
    monkeypatch.setattr(serve_mod, "post_webhook", lambda url, text: posted.append((url, text)) or True)

    runner = Runner(project, NotifyConfig(slack_webhook="https://x", on=["failure", "recovery"]),
                    sleeper=lambda s: None)
    sched = Schedule(name="j", cron="* * * * *", command=[str(script)], retries=2)
    runner.run_job(sched)

    assert (project / "attempts").read_text().strip() == "2"  # 1 fail + 1 success
    assert posted == []  # ended in success with no prior error -> no alert


def test_runner_exhausts_retries_and_alerts_failure(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    script = make_fake_dbt(project, fail_times=99)
    posted = []
    monkeypatch.setattr(serve_mod, "post_webhook", lambda url, text: posted.append(text) or True)

    runner = Runner(project, NotifyConfig(slack_webhook="https://x"), sleeper=lambda s: None)
    sched = Schedule(name="nightly", cron="* * * * *", command=[str(script)], retries=1)
    runner.run_job(sched)

    assert (project / "attempts").read_text().strip() == "2"  # initial + 1 retry
    assert len(posted) == 1 and "nightly" in posted[0] and "failed" in posted[0]
    # the failed run was still recorded
    conn = open_store(store_path(project))
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] >= 1
    conn.close()


def test_runner_recovery_alert(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    # Seed a previous error run
    conn = open_store(store_path(project))
    conn.execute("INSERT INTO runs (invocation_id, generated_at, env, status, imported_at) "
                 "VALUES ('old', '2000-01-01T00:00:00Z', 'default', 'error', 'x')")
    conn.commit()
    conn.close()
    script = make_fake_dbt(project, fail_times=0)
    posted = []
    monkeypatch.setattr(serve_mod, "post_webhook", lambda url, text: posted.append(text) or True)

    runner = Runner(project, NotifyConfig(teams_webhook="https://t"), sleeper=lambda s: None)
    runner.run_job(Schedule(name="j", cron="* * * * *", command=[str(script)]))
    assert len(posted) == 1 and "recovered" in posted[0]
