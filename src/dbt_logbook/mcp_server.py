"""MCP server: the history-backed agent surface over the store.

These tools answer the questions current-state tools (including the official
dbt-mcp) structurally cannot, because dbt overwrites its artifacts: what broke
last night, what got slower, what is flaky, what changed between runs.

Run with: dbt-logbook mcp   (stdio transport; point Claude/Cursor at it)
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import queries
from .store import open_store


def build_server(db_path: Path) -> FastMCP:
    mcp = FastMCP(
        "dbt-logbook",
        instructions=(
            "Run history for a dbt project. Every dbt invocation is recorded in a "
            "local store, so these tools can answer cross-run questions: failures "
            "and when they started, duration regressions, flaky models/tests, and "
            "what changed between any two runs."
        ),
    )

    def conn() -> sqlite3.Connection:
        return open_store(db_path)

    @mcp.tool()
    def get_run_history(limit: int = 20, env: str | None = None) -> dict:
        """Recent dbt runs, newest first: status, failure count, duration, env."""
        c = conn()
        try:
            return queries.run_history(c, limit=limit, env=env)
        finally:
            c.close()

    @mcp.tool()
    def what_broke(runs_back: int = 1) -> dict:
        """Failures in the most recent run(s), each flagged newly_broken if it
        passed the previous time it ran. Start here for 'what broke last night'."""
        c = conn()
        try:
            return queries.what_broke(c, runs_back=runs_back)
        finally:
            c.close()

    @mcp.tool()
    def get_model_history(model: str, limit: int = 30) -> dict:
        """Per-run status and duration for one model/test across time.
        `model` accepts a bare name (orders) or a full unique_id."""
        c = conn()
        try:
            uid = queries.resolve_node(c, model)
            if uid is None:
                return {"error": f"unknown node: {model}"}
            return {"unique_id": uid, "history": queries.model_history(c, uid, limit)}
        finally:
            c.close()

    @mcp.tool()
    def find_regressions(factor: float = 2.0, window: int = 10, min_seconds: float = 1.0) -> list[dict]:
        """Models whose latest duration is >= factor x the median of their
        previous runs (within `window`), ignoring sub-min_seconds noise."""
        c = conn()
        try:
            return queries.find_regressions(c, factor=factor, window=window, min_seconds=min_seconds)
        finally:
            c.close()

    @mcp.tool()
    def find_flaky_nodes(window: int = 20, min_flips: int = 2) -> list[dict]:
        """Nodes whose pass/fail status flipped repeatedly over recent runs -
        the tests you can't trust."""
        c = conn()
        try:
            return queries.flaky_nodes(c, window=window, min_flips=min_flips)
        finally:
            c.close()

    @mcp.tool()
    def diff_runs(run_a: str, run_b: str) -> dict:
        """Which nodes were added/removed/modified between two recorded runs
        (a=older, b=newer), keyed on dbt's own per-node checksums."""
        c = conn()
        try:
            try:
                return queries.diff_runs(c, run_a, run_b)
            except KeyError as e:
                return {"error": str(e)}
        finally:
            c.close()

    @mcp.tool()
    def what_changed() -> dict:
        """Convenience: diff the latest run against the one before it."""
        c = conn()
        try:
            rows = c.execute(
                "SELECT invocation_id FROM runs ORDER BY generated_at DESC LIMIT 2"
            ).fetchall()
            if len(rows) < 2:
                return {"error": "need at least two recorded runs"}
            try:
                return queries.diff_runs(c, rows[1]["invocation_id"], rows[0]["invocation_id"])
            except KeyError as e:
                return {"error": str(e)}
        finally:
            c.close()

    @mcp.tool()
    def state_modified_preview(env: str = "default", dbt_executable: str = "dbt") -> dict:
        """What would `--select state:modified` rebuild, compared against the
        last successful run recorded for `env`? Shells out to `dbt ls` (dbt must
        be installed and runnable in the project directory)."""
        if shutil.which(dbt_executable) is None:
            return {"error": f"'{dbt_executable}' not found on PATH - install dbt or pass dbt_executable"}
        c = conn()
        try:
            state_dir = Path(tempfile.mkdtemp(prefix="dbt-logbook-state-"))
            manifest = queries.export_last_good_manifest(c, env, state_dir)
        finally:
            c.close()
        if manifest is None:
            return {"error": f"no successful run with a manifest recorded for env '{env}'"}
        proc = subprocess.run(
            [dbt_executable, "ls", "--select", "state:modified",
             "--state", str(state_dir), "--output", "json"],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            return {"error": "dbt ls failed", "stderr": proc.stderr[-2000:]}
        nodes = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    nodes.append(json.loads(line).get("unique_id") or json.loads(line).get("name"))
                except json.JSONDecodeError:
                    continue
        return {"state_env": env, "would_rebuild": [n for n in nodes if n], "count": len(nodes)}

    return mcp
