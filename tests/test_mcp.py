"""MCP server: tool registration and round-trips through call_tool."""

import asyncio
import os
import stat

import pytest

from dbt_logbook.ingest import ingest_target_dir
from dbt_logbook.mcp_server import build_server
from dbt_logbook.paths import store_path
from dbt_logbook.store import open_store

EXPECTED_TOOLS = {
    "get_run_history",
    "what_broke",
    "get_model_history",
    "find_regressions",
    "find_flaky_nodes",
    "diff_runs",
    "what_changed",
    "state_modified_preview",
}


import json


def call(server, name, args=None):
    result = asyncio.run(server.call_tool(name, args or {}))
    # FastMCP returns (content, structured) in newer SDKs, a list of
    # TextContent (JSON in .text) in this one; normalize to the payload.
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


@pytest.fixture()
def project(tmp_path, target_dir_v1):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: p\nversion: '1.0'\n")
    conn = open_store(store_path(project))
    ingest_target_dir(conn, target_dir_v1)
    conn.close()
    return project


def test_all_tools_registered(project):
    server = build_server(store_path(project))
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_run_history_and_what_broke_roundtrip(project):
    server = build_server(store_path(project))
    hist = call(server, "get_run_history", {"limit": 5})
    assert hist["total"] == 1
    broke = call(server, "what_broke")
    assert broke["runs_examined"] == 1
    assert broke["failures"] == []


def test_model_history_accepts_bare_name(project):
    server = build_server(store_path(project))
    out = call(server, "get_model_history", {"model": "customers"})
    assert out["unique_id"] == "model.jaffle_shop.customers"
    assert len(out["history"]) == 1
    missing = call(server, "get_model_history", {"model": "nope"})
    assert "error" in missing


def test_what_changed_needs_two_runs(project):
    server = build_server(store_path(project))
    out = call(server, "what_changed")
    assert "error" in out


def test_state_modified_preview_with_fake_dbt(project, tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dbt = fake_bin / "dbt"
    fake_dbt.write_text(
        "#!/bin/sh\n"
        # dbt ls --select state:modified --state <dir> --output json
        'case "$*" in *"--state"*) ;; *) echo "missing --state" >&2; exit 1;; esac\n'
        'echo \'{"unique_id": "model.jaffle_shop.orders"}\'\n'
        'echo \'{"unique_id": "model.jaffle_shop.customers"}\'\n'
    )
    fake_dbt.chmod(fake_dbt.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    server = build_server(store_path(project))
    out = call(server, "state_modified_preview", {"env": "default"})
    assert out["count"] == 2
    assert "model.jaffle_shop.orders" in out["would_rebuild"]


def test_state_modified_preview_missing_dbt(project, monkeypatch):
    server = build_server(store_path(project))
    out = call(server, "state_modified_preview", {"dbt_executable": "definitely-not-dbt"})
    assert "not found" in out["error"]
