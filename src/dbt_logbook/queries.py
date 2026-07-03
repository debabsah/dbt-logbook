"""History queries shared by the REST API and the MCP server.

Every function takes an open sqlite connection and returns plain dicts/lists -
the single source of truth for what a "regression" or "flaky node" means, so
the two surfaces can never drift.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import statistics
from pathlib import Path

_FAILURE_STATUSES = ("error", "fail", "runtime error")
_FAIL_SQL = "lower(status) IN ('error','fail','runtime error')"


def run_history(conn: sqlite3.Connection, limit: int = 50, offset: int = 0,
                env: str | None = None) -> dict:
    where = "WHERE r.env = ?" if env else ""
    params: list = [env] if env else []
    rows = conn.execute(
        f"""SELECT r.invocation_id, r.generated_at, r.dbt_version, r.command,
                   r.env, r.status, r.elapsed, r.manifest_hash,
                   COUNT(nr.unique_id) AS nodes,
                   SUM(CASE WHEN lower(nr.status) IN {_FAILURE_STATUSES!r} THEN 1 ELSE 0 END) AS failed
            FROM runs r LEFT JOIN node_results nr USING (invocation_id)
            {where}
            GROUP BY r.invocation_id
            ORDER BY r.generated_at DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    return {"total": total, "runs": [dict(r) for r in rows]}


def what_broke(conn: sqlite3.Connection, runs_back: int = 1) -> dict:
    """Failures in the most recent run(s), with newly-broken detection.

    A node is "newly broken" if it failed in a recent run but passed the
    previous time it was executed.
    """
    recent = conn.execute(
        "SELECT invocation_id, generated_at, status FROM runs "
        "ORDER BY generated_at DESC LIMIT ?",
        (runs_back,),
    ).fetchall()
    if not recent:
        return {"runs_examined": 0, "failures": []}
    ids = [r["invocation_id"] for r in recent]
    q = ",".join("?" * len(ids))
    failures = conn.execute(
        f"""SELECT nr.invocation_id, r.generated_at, nr.unique_id, nr.status, nr.message
            FROM node_results nr JOIN runs r USING (invocation_id)
            WHERE nr.invocation_id IN ({q}) AND {_FAIL_SQL.replace('status', 'nr.status')}
            ORDER BY r.generated_at DESC""",
        ids,
    ).fetchall()
    out = []
    for f in failures:
        prev = conn.execute(
            f"""SELECT nr.status FROM node_results nr JOIN runs r USING (invocation_id)
                WHERE nr.unique_id = ? AND r.generated_at < ?
                ORDER BY r.generated_at DESC LIMIT 1""",
            (f["unique_id"], f["generated_at"]),
        ).fetchone()
        d = dict(f)
        d["newly_broken"] = prev is not None and str(prev["status"]).lower() not in _FAILURE_STATUSES
        out.append(d)
    return {"runs_examined": len(ids), "failures": out}


def model_history(conn: sqlite3.Connection, unique_id: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT nr.invocation_id, r.generated_at, r.env, nr.status,
                  nr.execution_time, nr.message
           FROM node_results nr JOIN runs r USING (invocation_id)
           WHERE nr.unique_id = ? ORDER BY r.generated_at DESC LIMIT ?""",
        (unique_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def resolve_node(conn: sqlite3.Connection, name_or_id: str) -> str | None:
    """Accept a full unique_id or a bare model name."""
    row = conn.execute(
        "SELECT unique_id FROM nodes_current WHERE unique_id = ? OR name = ? LIMIT 1",
        (name_or_id, name_or_id),
    ).fetchone()
    if row:
        return row["unique_id"]
    row = conn.execute(
        "SELECT DISTINCT unique_id FROM node_results WHERE unique_id = ? LIMIT 1",
        (name_or_id,),
    ).fetchone()
    return row["unique_id"] if row else None


def find_regressions(conn: sqlite3.Connection, factor: float = 2.0,
                     window: int = 10, min_seconds: float = 1.0) -> list[dict]:
    """Models whose latest duration exceeds `factor` x the median of the
    previous runs in `window`. min_seconds filters sub-second noise."""
    node_ids = [
        r["unique_id"]
        for r in conn.execute(
            "SELECT DISTINCT unique_id FROM node_results"
        ).fetchall()
    ]
    out = []
    for uid in node_ids:
        rows = conn.execute(
            """SELECT nr.execution_time FROM node_results nr
               JOIN runs r USING (invocation_id)
               WHERE nr.unique_id = ? AND nr.execution_time IS NOT NULL
               ORDER BY r.generated_at DESC LIMIT ?""",
            (uid, window),
        ).fetchall()
        times = [r["execution_time"] for r in rows]
        if len(times) < 3:
            continue
        latest, baseline = times[0], statistics.median(times[1:])
        if latest >= min_seconds and baseline > 0 and latest / baseline >= factor:
            out.append(
                {
                    "unique_id": uid,
                    "latest_seconds": round(latest, 3),
                    "baseline_median_seconds": round(baseline, 3),
                    "factor": round(latest / baseline, 2),
                    "runs_considered": len(times),
                }
            )
    return sorted(out, key=lambda x: -x["factor"])


def flaky_nodes(conn: sqlite3.Connection, window: int = 20, min_flips: int = 2) -> list[dict]:
    """Nodes whose pass/fail status flipped >= min_flips times in the last
    `window` runs. Checksum-blind by design: a code fix that repairs a node
    counts as one flip; refine if real usage needs better."""
    node_ids = [
        r["unique_id"]
        for r in conn.execute("SELECT DISTINCT unique_id FROM node_results").fetchall()
    ]
    out = []
    for uid in node_ids:
        rows = conn.execute(
            """SELECT nr.status FROM node_results nr JOIN runs r USING (invocation_id)
               WHERE nr.unique_id = ? ORDER BY r.generated_at DESC LIMIT ?""",
            (uid, window),
        ).fetchall()
        seq = [str(r["status"]).lower() in _FAILURE_STATUSES for r in reversed(rows)]
        flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        if flips >= min_flips:
            out.append(
                {
                    "unique_id": uid,
                    "flips": flips,
                    "runs_considered": len(seq),
                    "currently_failing": bool(seq and seq[-1]),
                }
            )
    return sorted(out, key=lambda x: -x["flips"])


def cost_summary(conn: sqlite3.Connection, rate_per_hour: float | None = None,
                 window_runs: int = 50, top: int = 20) -> dict:
    """Per-node compute spend over the last `window_runs` runs.

    Exact where the adapter reports volume (bytes_billed on BigQuery);
    estimated everywhere else as duration x rate_per_hour when a rate is
    configured. Share of total runtime is always available - it needs nothing.
    """
    recent = [
        r["invocation_id"]
        for r in conn.execute(
            "SELECT invocation_id FROM runs ORDER BY generated_at DESC LIMIT ?",
            (window_runs,),
        ).fetchall()
    ]
    if not recent:
        return {"window_runs": 0, "rate_per_hour": rate_per_hour, "nodes": []}
    q = ",".join("?" * len(recent))
    rows = conn.execute(
        f"""SELECT unique_id,
                   COUNT(*) AS runs,
                   SUM(COALESCE(execution_time, 0)) AS total_seconds,
                   SUM(bytes_billed) AS bytes_billed,
                   SUM(bytes_processed) AS bytes_processed
            FROM node_results WHERE invocation_id IN ({q})
            GROUP BY unique_id ORDER BY total_seconds DESC""",
        recent,
    ).fetchall()
    grand_total = sum(r["total_seconds"] or 0 for r in rows) or 1.0
    nodes = []
    for r in rows[:top]:
        secs = r["total_seconds"] or 0
        nodes.append(
            {
                "unique_id": r["unique_id"],
                "runs": r["runs"],
                "total_seconds": round(secs, 2),
                "share_pct": round(100 * secs / grand_total, 1),
                "est_cost": round(secs / 3600 * rate_per_hour, 2) if rate_per_hour else None,
                "bytes_billed": r["bytes_billed"],
                "bytes_processed": r["bytes_processed"],
            }
        )
    return {"window_runs": len(recent), "rate_per_hour": rate_per_hour, "nodes": nodes}


def freshness_history(conn: sqlite3.Connection, snapshots: int = 30) -> list[dict]:
    """Per source: the last N freshness snapshots, oldest first."""
    sources = [
        r["unique_id"]
        for r in conn.execute(
            "SELECT DISTINCT unique_id FROM source_freshness ORDER BY unique_id"
        ).fetchall()
    ]
    out = []
    for uid in sources:
        rows = conn.execute(
            "SELECT status, max_loaded_at, snapshotted_at FROM source_freshness "
            "WHERE unique_id = ? ORDER BY snapshotted_at DESC LIMIT ?",
            (uid, snapshots),
        ).fetchall()
        series = [dict(r) for r in reversed(rows)]
        out.append(
            {
                "unique_id": uid,
                "latest_status": series[-1]["status"] if series else None,
                "snapshots": series,
            }
        )
    return out


def load_manifest(conn: sqlite3.Connection, manifest_hash: str) -> dict | None:
    row = conn.execute(
        "SELECT gz FROM manifest_blobs WHERE hash = ?", (manifest_hash,)
    ).fetchone()
    return json.loads(gzip.decompress(row["gz"])) if row else None


def diff_runs(conn: sqlite3.Connection, a: str, b: str) -> dict:
    """Checksum-based node diff between two runs' manifests (a=older, b=newer)."""

    def checksums(invocation_id: str) -> tuple[dict, str]:
        run = conn.execute(
            "SELECT manifest_hash, dbt_version FROM runs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        if run is None:
            raise KeyError(f"run {invocation_id} not found")
        if run["manifest_hash"] is None:
            raise KeyError(f"run {invocation_id} has no manifest")
        manifest = load_manifest(conn, run["manifest_hash"])
        if manifest is None:
            raise KeyError(f"manifest for run {invocation_id} missing from store")
        out = {}
        for uid, node in (manifest.get("nodes") or {}).items():
            if isinstance(node, dict):
                cs = node.get("checksum") or {}
                out[uid] = cs.get("checksum") if isinstance(cs, dict) else None
        return out, run["dbt_version"] or ""

    ca, va = checksums(a)
    cb, vb = checksums(b)
    modified = sorted(uid for uid in set(ca) & set(cb) if ca[uid] != cb[uid])
    return {
        "a": a,
        "b": b,
        "added": sorted(set(cb) - set(ca)),
        "removed": sorted(set(ca) - set(cb)),
        "modified": modified,
        "unchanged": len(set(ca) & set(cb)) - len(modified),
        "engine_changed": va.split(".")[0] != vb.split(".")[0],
    }


def export_last_good_manifest(conn: sqlite3.Connection, env: str, dest_dir: Path) -> Path | None:
    """Write the last successful run's manifest for `env` to dest_dir/manifest.json
    (the state dir shape `dbt --state` expects). Returns the path or None."""
    row = conn.execute(
        "SELECT manifest_hash FROM runs "
        "WHERE env = ? AND status = 'success' AND manifest_hash IS NOT NULL "
        "ORDER BY generated_at DESC LIMIT 1",
        (env,),
    ).fetchone()
    if row is None:
        return None
    manifest = load_manifest(conn, row["manifest_hash"])
    if manifest is None:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "manifest.json"
    path.write_text(json.dumps(manifest, default=str))
    return path
