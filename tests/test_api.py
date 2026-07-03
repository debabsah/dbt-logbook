import json

import pytest
from fastapi.testclient import TestClient

from dbt_logbook.api import create_app
from dbt_logbook.ingest import ingest_target_dir
from dbt_logbook.store import open_store

from conftest import load_json


@pytest.fixture()
def client_empty(tmp_path):
    db = tmp_path / "history.db"
    open_store(db).close()
    return TestClient(create_app(db))


@pytest.fixture()
def client_two_runs(tmp_path, target_dir_v1):
    """Store with two runs: run B has one modified model and a failure."""
    db = tmp_path / "history.db"
    conn = open_store(db)
    ingest_target_dir(conn, target_dir_v1)

    manifest = load_json(target_dir_v1 / "manifest.json")
    rr = load_json(target_dir_v1 / "run_results.json")
    model_id = next(
        k for k, v in manifest["nodes"].items() if v.get("resource_type") == "model"
    )
    manifest["nodes"][model_id]["checksum"]["checksum"] = "changed-checksum"
    for doc in (manifest, rr):
        doc["metadata"]["invocation_id"] = "run-b"
        doc["metadata"]["generated_at"] = "2999-01-01T00:00:00Z"
    rr["results"][0]["status"] = "error"
    (target_dir_v1 / "manifest.json").write_text(json.dumps(manifest))
    (target_dir_v1 / "run_results.json").write_text(json.dumps(rr))
    ingest_target_dir(conn, target_dir_v1)
    conn.close()
    return TestClient(create_app(db)), model_id


def test_empty_store_all_endpoints_200(client_empty):
    assert client_empty.get("/api/summary").json()["runs"] == 0
    assert client_empty.get("/api/runs").json() == {"total": 0, "runs": []}
    dag = client_empty.get("/api/dag").json()
    assert dag["nodes"] == [] and dag["too_large"] is False


def test_runs_timeline(client_two_runs):
    client, _ = client_two_runs
    data = client.get("/api/runs").json()
    assert data["total"] == 2
    newest = data["runs"][0]
    assert newest["invocation_id"] == "run-b"
    assert newest["status"] == "error"
    assert newest["failed"] >= 1
    assert newest["nodes"] > 0


def test_run_detail_and_404(client_two_runs):
    client, _ = client_two_runs
    detail = client.get("/api/runs/run-b").json()
    assert detail["results"], "node results expected"
    assert "run_results_gz" not in detail
    assert client.get("/api/runs/nope").status_code == 404


def test_model_history_sparkline_data(client_two_runs):
    client, model_id = client_two_runs
    d = client.get(f"/api/models/{model_id}").json()
    assert len(d["history"]) == 2
    assert d["node"]["checksum"] == "changed-checksum"
    assert client.get("/api/models/model.nope.x").status_code == 404


def test_model_sql_from_blob(client_two_runs):
    client, model_id = client_two_runs
    sql = client.get(f"/api/models/{model_id}/sql").json()
    assert sql["raw_code"]


def test_diff_checksum_based(client_two_runs):
    client, model_id = client_two_runs
    runs = client.get("/api/runs").json()["runs"]
    older = runs[1]["invocation_id"]
    d = client.get(f"/api/diff?a={older}&b=run-b").json()
    assert d["modified"] == [model_id]
    assert d["added"] == [] and d["removed"] == []
    assert d["unchanged"] > 0
    assert d["engine_changed"] is False


def test_dag_whole_graph_and_neighborhood(client_two_runs):
    client, model_id = client_two_runs
    whole = client.get("/api/dag").json()
    assert whole["too_large"] is False
    assert any(n["id"] == model_id for n in whole["nodes"])
    assert whole["edges"], "jaffle shop has lineage edges"

    hood = client.get(f"/api/dag?node={model_id}&hops=1").json()
    assert any(n["id"] == model_id for n in hood["nodes"])
    assert len(hood["nodes"]) <= len(whole["nodes"])


def test_index_serves_ui(client_two_runs):
    client, _ = client_two_runs
    assert "dbt-logbook" in client.get("/").text
