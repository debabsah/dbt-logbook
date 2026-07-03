"""exec wrapper: exit-code passthrough, capture-on-failure, stale guard.

A fake "dbt" (a tiny shell script) stands in for the real thing: it copies
golden artifacts into target/ (patching invocation_id + generated_at so they
postdate the wrapper's start) and exits with a chosen code.
"""

import json
import sqlite3
import stat
from pathlib import Path

from dbt_logbook.exec_wrapper import run_wrapped
from dbt_logbook.paths import store_path

FIXTURES = Path(__file__).parent / "fixtures" / "dbt-1.11"


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: fake\nversion: '1.0'\n")
    return project


def make_fake_dbt(project: Path, exit_code: int, write_artifacts: bool = True) -> Path:
    """A script that (optionally) writes fresh artifacts, then exits."""
    target = project / "target"
    script = project / "fake_dbt.sh"
    lines = ["#!/bin/sh"]
    if write_artifacts:
        lines.append(f"mkdir -p {target}")
        # Artifacts are staged by the test with fresh timestamps; the script
        # just moves them into place, as dbt would write them at run time.
        lines.append(f"cp {project}/staged/*.json {target}/")
    lines.append(f"exit {exit_code}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def stage_artifacts(project: Path, invocation_id: str, generated_at: str) -> None:
    staged = project / "staged"
    staged.mkdir(exist_ok=True)
    for name in ("manifest.json", "run_results.json"):
        doc = json.loads((FIXTURES / name).read_text())
        doc.setdefault("metadata", {})["invocation_id"] = invocation_id
        doc["metadata"]["generated_at"] = generated_at
        (staged / name).write_text(json.dumps(doc))


def run_count(project: Path) -> int:
    conn = sqlite3.connect(store_path(project))
    try:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        conn.close()


def test_exit_code_passthrough_success(tmp_path):
    project = make_project(tmp_path)
    stage_artifacts(project, "run-ok", "2999-01-01T00:00:00Z")
    script = make_fake_dbt(project, exit_code=0)
    code, result = run_wrapped([str(script)], project)
    assert code == 0
    assert result.status == "ingested"
    assert run_count(project) == 1


def test_exit_code_passthrough_failure_still_captures(tmp_path):
    project = make_project(tmp_path)
    stage_artifacts(project, "run-failed", "2999-01-01T00:00:00Z")
    script = make_fake_dbt(project, exit_code=2)
    code, result = run_wrapped([str(script)], project)
    assert code == 2, "dbt's exit code must pass through for cron/CI alerting"
    assert result.status == "ingested", "failed runs are the most valuable history"
    assert run_count(project) == 1


def test_stale_artifacts_not_attributed(tmp_path):
    """dbt crashes before writing: leftover artifacts from a PREVIOUS run
    (old generated_at) must not be recorded as this invocation."""
    project = make_project(tmp_path)
    stage_artifacts(project, "previous-run", "2000-01-01T00:00:00Z")
    # Pre-place the stale artifacts, then have "dbt" write nothing and die.
    (project / "target").mkdir()
    for f in (project / "staged").glob("*.json"):
        (project / "target" / f.name).write_text(f.read_text())
    script = make_fake_dbt(project, exit_code=1, write_artifacts=False)

    code, result = run_wrapped([str(script)], project)
    assert code == 1
    assert result.status == "stale"
    assert run_count(project) == 0


def test_missing_command(tmp_path):
    project = make_project(tmp_path)
    code, result = run_wrapped(["/nonexistent/dbt"], project)
    assert code == 127
    assert result.status == "no_artifacts"
