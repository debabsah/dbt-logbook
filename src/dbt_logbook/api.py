"""FastAPI app: JSON views over the history store + the static UI shell.

Every endpoint is a read-only view over SQLite. The connection is opened per
request (SQLite is cheap to open; WAL allows concurrent readers with writers).
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).parent / "web"


def _load_manifest(conn: sqlite3.Connection, manifest_hash: str) -> dict:
    row = conn.execute(
        "SELECT gz FROM manifest_blobs WHERE hash = ?", (manifest_hash,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"manifest {manifest_hash} not in store")
    return json.loads(gzip.decompress(row["gz"]))


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="dbt-logbook", docs_url=None, redoc_url=None)

    def db() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @app.get("/api/summary")
    def summary():
        conn = db()
        try:
            runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            models = conn.execute(
                "SELECT COUNT(*) FROM nodes_current WHERE resource_type='model'"
            ).fetchone()[0]
            last = conn.execute(
                "SELECT invocation_id, generated_at, status, env FROM runs "
                "ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
            return {
                "runs": runs,
                "models": models,
                "last_run": dict(last) if last else None,
            }
        finally:
            conn.close()

    @app.get("/api/runs")
    def runs(limit: int = Query(50, le=500), offset: int = 0):
        conn = db()
        try:
            rows = conn.execute(
                "SELECT r.invocation_id, r.generated_at, r.dbt_version, r.command, "
                "       r.env, r.status, r.elapsed, r.manifest_hash, "
                "       COUNT(nr.unique_id) AS nodes, "
                "       SUM(CASE WHEN lower(nr.status) IN ('error','fail','runtime error') "
                "           THEN 1 ELSE 0 END) AS failed "
                "FROM runs r LEFT JOIN node_results nr USING (invocation_id) "
                "GROUP BY r.invocation_id "
                "ORDER BY r.generated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            return {"total": total, "runs": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/api/runs/{invocation_id}")
    def run_detail(invocation_id: str):
        conn = db()
        try:
            run = conn.execute(
                "SELECT * FROM runs WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
            if run is None:
                raise HTTPException(404, "run not found")
            nodes = conn.execute(
                "SELECT unique_id, status, execution_time, message, rows_affected "
                "FROM node_results WHERE invocation_id = ? "
                "ORDER BY execution_time DESC",
                (invocation_id,),
            ).fetchall()
            out = dict(run)
            out.pop("run_results_gz", None)
            out["results"] = [dict(n) for n in nodes]
            return out
        finally:
            conn.close()

    @app.get("/api/models/{unique_id}")
    def model_detail(unique_id: str):
        conn = db()
        try:
            node = conn.execute(
                "SELECT * FROM nodes_current WHERE unique_id = ?", (unique_id,)
            ).fetchone()
            history = conn.execute(
                "SELECT nr.invocation_id, r.generated_at, nr.status, "
                "       nr.execution_time, nr.message "
                "FROM node_results nr JOIN runs r USING (invocation_id) "
                "WHERE nr.unique_id = ? ORDER BY r.generated_at",
                (unique_id,),
            ).fetchall()
            if node is None and not history:
                raise HTTPException(404, "unknown node")
            return {
                "node": dict(node) if node else None,
                "history": [dict(h) for h in history],
            }
        finally:
            conn.close()

    @app.get("/api/models/{unique_id}/sql")
    def model_sql(unique_id: str):
        conn = db()
        try:
            row = conn.execute(
                "SELECT manifest_hash FROM runs WHERE manifest_hash IS NOT NULL "
                "ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise HTTPException(404, "no manifest in store")
            manifest = _load_manifest(conn, row["manifest_hash"])
            node = (manifest.get("nodes") or {}).get(unique_id)
            if node is None:
                raise HTTPException(404, "node not in latest manifest")
            return {
                "unique_id": unique_id,
                "raw_code": node.get("raw_code") or node.get("raw_sql"),
                "compiled_code": node.get("compiled_code"),
            }
        finally:
            conn.close()

    @app.get("/api/diff")
    def diff(a: str, b: str):
        """Checksum-based node diff between two runs' manifests (a=older, b=newer)."""
        conn = db()
        try:
            def checksums(invocation_id: str) -> tuple[dict, str | None]:
                run = conn.execute(
                    "SELECT manifest_hash, dbt_version FROM runs WHERE invocation_id = ?",
                    (invocation_id,),
                ).fetchone()
                if run is None:
                    raise HTTPException(404, f"run {invocation_id} not found")
                if run["manifest_hash"] is None:
                    raise HTTPException(404, f"run {invocation_id} has no manifest")
                manifest = _load_manifest(conn, run["manifest_hash"])
                out = {}
                for uid, node in (manifest.get("nodes") or {}).items():
                    if isinstance(node, dict):
                        cs = node.get("checksum") or {}
                        out[uid] = cs.get("checksum") if isinstance(cs, dict) else None
                return out, run["dbt_version"]

            ca, va = checksums(a)
            cb, vb = checksums(b)
            added = sorted(set(cb) - set(ca))
            removed = sorted(set(ca) - set(cb))
            modified = sorted(
                uid for uid in set(ca) & set(cb) if ca[uid] != cb[uid]
            )
            unchanged = len(set(ca) & set(cb)) - len(modified)
            return {
                "a": a,
                "b": b,
                "added": added,
                "removed": removed,
                "modified": modified,
                "unchanged": unchanged,
                # A v1->v2 engine change re-hashes everything; the UI shows a banner.
                "engine_changed": (va or "").split(".")[0] != (vb or "").split(".")[0],
            }
        finally:
            conn.close()

    @app.get("/api/dag")
    def dag(
        node: str | None = None,
        hops: int = Query(2, le=5),
        tests: bool = Query(False, description="Include test nodes (off: cleaner lineage)."),
    ):
        """Whole graph if small; otherwise the +-N-hop neighborhood of `node`."""
        conn = db()
        try:
            rows = conn.execute(
                "SELECT unique_id, name, resource_type, depends_on FROM nodes_current"
                + ("" if tests else " WHERE resource_type != 'test'")
            ).fetchall()
        finally:
            conn.close()
        parents = {
            r["unique_id"]: json.loads(r["depends_on"] or "[]") for r in rows
        }
        children: dict[str, list[str]] = {}
        for uid, deps in parents.items():
            for d in deps:
                children.setdefault(d, []).append(uid)
        info = {r["unique_id"]: r for r in rows}

        if node is None:
            if len(rows) > 300:
                return {
                    "too_large": True,
                    "count": len(rows),
                    "nodes": [],
                    "edges": [],
                }
            keep = set(info)
        else:
            keep = {node}
            frontier = {node}
            for _ in range(hops):
                nxt = set()
                for uid in frontier:
                    nxt.update(parents.get(uid, []))
                    nxt.update(children.get(uid, []))
                nxt -= keep
                keep |= nxt
                frontier = nxt

        nodes_out = [
            {
                "id": uid,
                "name": info[uid]["name"] if uid in info else uid.split(".")[-1],
                "type": info[uid]["resource_type"] if uid in info else "source",
            }
            for uid in keep
        ]
        edges_out = [
            {"source": dep, "target": uid}
            for uid in keep
            for dep in parents.get(uid, [])
            if dep in keep
        ]
        return {"too_large": False, "count": len(rows), "nodes": nodes_out, "edges": edges_out}

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    return app
