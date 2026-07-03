import json

from dbt_logbook.ingest import (
    ingest_target_dir,
    normalized_manifest_hash,
)

from conftest import load_json


def test_ingest_happy_path_both_versions(store, target_dir):
    result = ingest_target_dir(store, target_dir)
    assert result.status == "ingested"
    assert result.invocation_id

    run = store.execute("SELECT * FROM runs").fetchone()
    assert run["invocation_id"] == result.invocation_id
    assert run["status"] == "success"
    assert run["manifest_hash"]
    assert run["dbt_version"]

    node_count = store.execute("SELECT COUNT(*) FROM node_results").fetchone()[0]
    assert node_count > 0

    node = store.execute(
        "SELECT * FROM nodes_current WHERE resource_type='model' LIMIT 1"
    ).fetchone()
    assert node["checksum"]
    assert json.loads(node["depends_on"]) is not None


def test_ingest_is_idempotent(store, target_dir):
    first = ingest_target_dir(store, target_dir)
    second = ingest_target_dir(store, target_dir)
    assert first.status == "ingested"
    assert second.status == "duplicate"
    assert store.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_empty_target_dir(store, tmp_path):
    empty = tmp_path / "empty_target"
    empty.mkdir()
    result = ingest_target_dir(store, empty)
    assert result.status == "no_artifacts"


def test_corrupt_artifacts_never_crash(store, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "run_results.json").write_text('{"metadata": {"invocation')  # truncated
    (target / "manifest.json").write_text("also not json")
    result = ingest_target_dir(store, target)
    assert result.status == "corrupt"
    assert store.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_manifest_only_refreshes_node_index(store, target_dir_v1):
    (target_dir_v1 / "run_results.json").unlink()
    result = ingest_target_dir(store, target_dir_v1)
    assert result.status == "manifest_only"
    assert store.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert store.execute("SELECT COUNT(*) FROM nodes_current").fetchone()[0] > 0


def test_stale_artifacts_refused(store, target_dir_v1):
    result = ingest_target_dir(store, target_dir_v1, not_before="2999-01-01T00:00:00Z")
    assert result.status == "stale"
    assert store.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_unknown_future_schema_degrades_gracefully(store, target_dir_v1):
    rr = load_json(target_dir_v1 / "run_results.json")
    rr.pop("args", None)
    rr.pop("elapsed_time", None)
    for r in rr.get("results", []):
        r.pop("adapter_response", None)
        r["novel_field_from_dbt_v9"] = {"nested": True}
    (target_dir_v1 / "run_results.json").write_text(json.dumps(rr))

    result = ingest_target_dir(store, target_dir_v1)
    assert result.status == "ingested"
    run = store.execute("SELECT * FROM runs").fetchone()
    assert run["env"] == "default"  # args gone -> fallback


def test_env_override_beats_args_target(store, target_dir_v1):
    result = ingest_target_dir(store, target_dir_v1, env="prod")
    assert result.status == "ingested"
    assert store.execute("SELECT env FROM runs").fetchone()["env"] == "prod"


def test_failed_run_recorded_as_error(store, target_dir_v1):
    rr = load_json(target_dir_v1 / "run_results.json")
    rr["results"][0]["status"] = "error"
    rr["results"][0]["message"] = "Compilation Error in model X"
    (target_dir_v1 / "run_results.json").write_text(json.dumps(rr))

    result = ingest_target_dir(store, target_dir_v1)
    assert result.status == "ingested"
    assert store.execute("SELECT status FROM runs").fetchone()["status"] == "error"


def test_normalized_hash_stable_across_volatile_churn(target_dir_v1):
    manifest = load_json(target_dir_v1 / "manifest.json")
    h1 = normalized_manifest_hash(manifest)

    manifest["metadata"]["generated_at"] = "2999-01-01T00:00:00Z"
    manifest["metadata"]["invocation_id"] = "different-invocation"
    for node in manifest["nodes"].values():
        if isinstance(node, dict) and "created_at" in node:
            node["created_at"] = 9999999999.0
    assert normalized_manifest_hash(manifest) == h1

    # A real content change must change the hash.
    model_id = next(
        k for k, v in manifest["nodes"].items() if v.get("resource_type") == "model"
    )
    manifest["nodes"][model_id]["checksum"]["checksum"] = "deadbeef"
    assert normalized_manifest_hash(manifest) != h1


def test_manifest_blob_dedup_across_runs(store, target_dir_v1):
    ingest_target_dir(store, target_dir_v1)

    rr = load_json(target_dir_v1 / "run_results.json")
    rr["metadata"]["invocation_id"] = "second-run-different-id"
    (target_dir_v1 / "run_results.json").write_text(json.dumps(rr))
    manifest = load_json(target_dir_v1 / "manifest.json")
    manifest["metadata"]["invocation_id"] = "second-run-different-id"
    for node in manifest["nodes"].values():
        if isinstance(node, dict) and "created_at" in node:
            node["created_at"] = 1234567890.0
    (target_dir_v1 / "manifest.json").write_text(json.dumps(manifest))

    result = ingest_target_dir(store, target_dir_v1)
    assert result.status == "ingested"
    assert store.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    assert store.execute("SELECT COUNT(*) FROM manifest_blobs").fetchone()[0] == 1
