"""v0.6: cost signals (Tier 1) and ci-report (Tier 2)."""

import json
import sqlite3

from dbt_logbook import queries
from dbt_logbook.ci_report import LocalHistory, build_report, to_markdown
from dbt_logbook.ingest import ingest_target_dir
from dbt_logbook.store import open_store

from conftest import load_json


# ---------- Tier 1: cost ----------

def test_bytes_extracted_from_adapter_response(store, target_dir_v1):
    rr = load_json(target_dir_v1 / "run_results.json")
    model_res = next(r for r in rr["results"] if r["unique_id"].startswith("model."))
    model_res["adapter_response"]["bytes_processed"] = 5_000_000_000
    model_res["adapter_response"]["bytes_billed"] = 6_000_000_000
    (target_dir_v1 / "run_results.json").write_text(json.dumps(rr))

    ingest_target_dir(store, target_dir_v1)
    row = store.execute(
        "SELECT bytes_processed, bytes_billed FROM node_results WHERE unique_id = ?",
        (model_res["unique_id"],),
    ).fetchone()
    assert row["bytes_processed"] == 5_000_000_000
    assert row["bytes_billed"] == 6_000_000_000


def test_cost_summary_share_and_estimates(store, target_dir_v1):
    ingest_target_dir(store, target_dir_v1)
    out = queries.cost_summary(store, rate_per_hour=3.6, window_runs=10)
    assert out["window_runs"] == 1
    assert out["nodes"], "expected per-node rows"
    top = out["nodes"][0]
    assert top["share_pct"] > 0
    assert top["est_cost"] is not None  # rate configured -> estimate present
    no_rate = queries.cost_summary(store, rate_per_hour=None)
    assert no_rate["nodes"][0]["est_cost"] is None  # no rate -> no fake dollars


def test_v2_store_migrates_to_v3(tmp_path):
    import dbt_logbook.store as store_mod

    db = tmp_path / "history.db"
    conn = sqlite3.connect(db)
    conn.executescript(store_mod._SCHEMA_V1)
    conn.executescript(store_mod._SCHEMA_V2)
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
    conn.execute(
        "INSERT INTO runs (invocation_id, status, imported_at) VALUES ('r', 'success', 'x')"
    )
    conn.execute(
        "INSERT INTO node_results (invocation_id, unique_id, status) VALUES ('r', 'm.x', 'success')"
    )
    conn.commit()
    conn.close()

    conn = open_store(db)
    row = conn.execute(
        "SELECT unique_id, bytes_billed FROM node_results"
    ).fetchone()
    assert row["unique_id"] == "m.x" and row["bytes_billed"] is None
    assert db.with_suffix(".db.bak").exists()
    conn.close()


# ---------- Tier 2: ci-report ----------

def two_run_history(store, target_dir_v1, tmp_path):
    """Store has one good baseline run; PR target has one modified model,
    one failure, one regression."""
    ingest_target_dir(store, target_dir_v1)

    pr_target = tmp_path / "pr_target"
    pr_target.mkdir()
    manifest = load_json(target_dir_v1 / "manifest.json")
    rr = load_json(target_dir_v1 / "run_results.json")
    model_ids = sorted(
        k for k, v in manifest["nodes"].items() if v.get("resource_type") == "model"
    )
    changed, failed, slow = model_ids[0], model_ids[1], model_ids[2]
    manifest["nodes"][changed]["checksum"]["checksum"] = "pr-changed"
    for res in rr["results"]:
        if res["unique_id"] == failed:
            res["status"] = "error"
            res["message"] = "Compilation Error: bad ref"
        if res["unique_id"] == slow:
            res["execution_time"] = 50.0  # baseline median will be tiny
    for doc in (manifest, rr):
        doc["metadata"]["invocation_id"] = "pr-run"
    (pr_target / "manifest.json").write_text(json.dumps(manifest))
    (pr_target / "run_results.json").write_text(json.dumps(rr))
    return pr_target, changed, failed, slow


def test_ci_report_full_story(store, target_dir_v1, tmp_path):
    pr_target, changed, failed, slow = two_run_history(store, target_dir_v1, tmp_path)
    report = build_report(pr_target, LocalHistory(store, "default"), cost_rate=3600.0)

    assert report.state_found
    assert report.modified == [changed]
    assert report.impacted, "changed staging model must impact downstream marts"
    assert [f["unique_id"] for f in report.failures] == [failed]
    assert report.verdict == "failing"
    reg_ids = [g["unique_id"] for g in report.regressions]
    assert slow in reg_ids
    slow_entry = next(g for g in report.regressions if g["unique_id"] == slow)
    assert slow_entry["extra_cost_per_run"] > 0

    md = to_markdown(report, "default")
    assert "dbt-logbook-ci-report" in md  # upsert marker
    assert "failing" in md and changed.split(".")[-1] in md
    assert "Slower than their history" in md


def test_ci_report_clean_run(store, target_dir_v1, tmp_path):
    ingest_target_dir(store, target_dir_v1)
    report = build_report(target_dir_v1, LocalHistory(store, "default"))
    assert report.verdict == "clean"
    assert report.modified == [] and report.failures == []
    assert "looks safe" in to_markdown(report, "default")


def test_ci_report_no_baseline(store, tmp_path, target_dir_v1):
    # empty store: no last-good state for env
    report = build_report(target_dir_v1, LocalHistory(store, "prod"))
    assert report.verdict == "no-state"
    assert "no baseline" in to_markdown(report, "prod")


def test_ci_report_remote_history(store, target_dir_v1, tmp_path, monkeypatch):
    """RemoteHistory drives the same report through monkeypatched HTTP."""
    import dbt_logbook.ci_report as cr

    pr_target, changed, failed, slow = two_run_history(store, target_dir_v1, tmp_path)
    local = LocalHistory(store, "default")

    class FakeRemote(cr.RemoteHistory):
        def _get(self, path):
            if path.startswith("/api/state/"):
                row = store.execute(
                    "SELECT manifest_hash FROM runs WHERE status='success' LIMIT 1"
                ).fetchone()
                return queries.load_manifest(store, row["manifest_hash"])
            if path.startswith("/api/models/"):
                uid = path.split("/api/models/")[1]
                return {"history": queries.model_history(store, uid)}
            return None

    remote = FakeRemote("http://logbook.internal", "default", token="t")
    report = build_report(pr_target, remote)
    assert report.modified == [changed]
    assert report.verdict == "failing"
