"""v0.4: CI state serving + token auth."""

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from dbt_logbook.api import create_app
from dbt_logbook.cli import app as cli_app
from dbt_logbook.ingest import ingest_target_dir
from dbt_logbook.store import open_store


def seeded_db(tmp_path, target_dir_v1):
    db = tmp_path / "history.db"
    conn = open_store(db)
    ingest_target_dir(conn, target_dir_v1)
    conn.close()
    return db


def test_state_endpoint_serves_last_good_manifest(tmp_path, target_dir_v1):
    client = TestClient(create_app(seeded_db(tmp_path, target_dir_v1)))
    r = client.get("/api/state/default/manifest.json")
    assert r.status_code == 200
    assert r.json()["nodes"]
    assert client.get("/api/state/no_such_env/manifest.json").status_code == 404


def test_token_auth_gates_api_only_when_configured(tmp_path, target_dir_v1):
    db = seeded_db(tmp_path, target_dir_v1)

    open_client = TestClient(create_app(db))
    assert open_client.get("/api/summary").status_code == 200

    client = TestClient(create_app(db, token="s3cret"))
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/summary", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get(
        "/api/state/default/manifest.json", headers={"Authorization": "Bearer s3cret"}
    ).status_code == 200
    # static shell stays reachable (auth covers /api/* only)
    assert client.get("/").status_code == 200


def test_serve_refuses_non_localhost_without_token(tmp_path, target_dir_v1, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: p\nversion: '1.0'\n")
    monkeypatch.chdir(project)
    monkeypatch.delenv("DBT_LOGBOOK_TOKEN", raising=False)
    result = CliRunner().invoke(cli_app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "requires --token" in result.output


def test_state_cli_exports_manifest(tmp_path, target_dir_v1, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: p\nversion: '1.0'\n")
    conn = open_store(project / ".dbtlogbook" / "history.db")
    ingest_target_dir(conn, target_dir_v1)
    conn.close()
    monkeypatch.chdir(project)

    result = CliRunner().invoke(cli_app, ["state", "--env", "default", "--out", "ci-state"])
    assert result.exit_code == 0
    manifest = json.loads((project / "ci-state" / "manifest.json").read_text())
    assert manifest["nodes"]

    missing = CliRunner().invoke(cli_app, ["state", "--env", "nope"])
    assert missing.exit_code == 1
