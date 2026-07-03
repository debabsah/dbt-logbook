"""The single ingest path shared by every command (ui / import / exec / demo).

Tolerant narrow extraction: pull the ~dozen shallow fields we need with .get(),
keep the raw gzipped JSON so anything missed is recoverable later. Never
validate whole documents against a schema - a new dbt version at worst yields
None fields, never a crash. Verified against golden artifacts of dbt 1.7
through 2.0 (docs/compatibility.md).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Stripped before hashing a manifest for blob dedup. metadata churns per
# invocation; per-node created_at/build_path churn per full re-parse (dbt 1.x;
# gone in v2). Node CHANGE detection uses dbt's own per-node checksum instead.
_VOLATILE_TOP = ("metadata",)
_VOLATILE_NODE = ("created_at", "build_path")

_FAILURE_STATUSES = {"error", "fail", "runtime error"}


@dataclass
class IngestResult:
    status: str  # ingested | duplicate | manifest_only | no_artifacts | stale | corrupt
    invocation_id: str | None = None
    detail: str = ""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path):
    """Parsed JSON, or None for a missing, partial, or corrupt file."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _gz(payload: dict) -> bytes:
    return gzip.compress(
        json.dumps(payload, separators=(",", ":"), default=str).encode()
    )


def normalized_manifest_hash(manifest: dict) -> str:
    """Content hash that is stable across re-parses of the same logical project."""
    slim = {k: v for k, v in manifest.items() if k not in _VOLATILE_TOP}
    for section in ("nodes", "sources", "exposures", "semantic_models"):
        nodes = slim.get(section)
        if isinstance(nodes, dict):
            slim[section] = {
                nid: (
                    {k: v for k, v in node.items() if k not in _VOLATILE_NODE}
                    if isinstance(node, dict)
                    else node
                )
                for nid, node in nodes.items()
            }
    payload = json.dumps(slim, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _store_manifest(conn: sqlite3.Connection, manifest: dict) -> str:
    h = normalized_manifest_hash(manifest)
    conn.execute(
        "INSERT OR IGNORE INTO manifest_blobs (hash, gz, node_count, created_at) VALUES (?, ?, ?, ?)",
        (h, _gz(manifest), len(manifest.get("nodes") or {}), _utcnow()),
    )
    return h


def _ingest_sources(conn: sqlite3.Connection, sources: dict) -> None:
    """Record `dbt source freshness` results (sources.json). Tolerant and
    idempotent like everything else."""
    meta = sources.get("metadata") or {}
    invocation_id = meta.get("invocation_id")
    if not invocation_id:
        return
    rows = []
    for r in sources.get("results") or []:
        if isinstance(r, dict) and r.get("unique_id"):
            rows.append(
                (
                    invocation_id,
                    r.get("unique_id"),
                    r.get("status"),
                    r.get("max_loaded_at"),
                    r.get("snapshotted_at") or meta.get("generated_at"),
                )
            )
    conn.executemany(
        "INSERT OR IGNORE INTO source_freshness "
        "(invocation_id, unique_id, status, max_loaded_at, snapshotted_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def _refresh_nodes_current(conn: sqlite3.Connection, manifest: dict) -> None:
    nodes = manifest.get("nodes")
    if not isinstance(nodes, dict):
        return
    rows = []
    for unique_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        checksum = node.get("checksum") or {}
        depends = node.get("depends_on") or {}
        rows.append(
            (
                unique_id,
                node.get("name"),
                node.get("resource_type"),
                checksum.get("checksum") if isinstance(checksum, dict) else None,
                json.dumps(depends.get("nodes") or []) if isinstance(depends, dict) else "[]",
                node.get("original_file_path") or node.get("path"),
                node.get("description"),
            )
        )
    with conn:
        conn.execute("DELETE FROM nodes_current")
        conn.executemany(
            "INSERT OR REPLACE INTO nodes_current "
            "(unique_id, name, resource_type, checksum, depends_on, path, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def ingest_target_dir(
    conn: sqlite3.Connection,
    target_dir: Path,
    env: str | None = None,
    not_before: str | None = None,
) -> IngestResult:
    """Idempotently ingest one target directory's artifacts.

    not_before: ISO timestamp; artifacts generated before it are refused
    ("stale") - the exec wrapper uses this so a crashed dbt can't get the
    previous run's artifacts attributed to it.
    """
    run_results = _read_json(target_dir / "run_results.json")
    manifest = _read_json(target_dir / "manifest.json")
    sources = _read_json(target_dir / "sources.json")
    if sources:
        _ingest_sources(conn, sources)

    if run_results is None and manifest is None:
        if sources:
            conn.commit()
            return IngestResult("sources_only", detail="source freshness recorded")
        exists = (target_dir / "run_results.json").exists() or (
            target_dir / "manifest.json"
        ).exists()
        if exists:
            return IngestResult("corrupt", detail="artifact files present but unparseable")
        return IngestResult("no_artifacts", detail=f"no artifacts in {target_dir}")

    if run_results is None:
        _store_manifest(conn, manifest)
        _refresh_nodes_current(conn, manifest)
        conn.commit()
        return IngestResult("manifest_only", detail="node index refreshed; no run recorded")

    meta = run_results.get("metadata") or {}
    invocation_id = meta.get("invocation_id")
    generated_at = meta.get("generated_at")
    if not invocation_id:
        return IngestResult("corrupt", detail="run_results.json missing metadata.invocation_id")
    # Compare on the first 19 chars ("YYYY-MM-DDTHH:MM:SS") - both sides are
    # UTC ISO but dbt uses a trailing Z while we use +00:00.
    if not_before and generated_at and str(generated_at)[:19] < str(not_before)[:19]:
        return IngestResult(
            "stale", invocation_id, "artifacts predate wrapper start; not attributed"
        )

    args = run_results.get("args") or {}
    env_name = env or args.get("target") or "default"
    command = args.get("invocation_command") or args.get("which") or ""
    results = run_results.get("results") or []
    status = "success"
    node_rows = []
    for r in results:
        if not isinstance(r, dict):
            continue
        if str(r.get("status")).lower() in _FAILURE_STATUSES:
            status = "error"
        adapter = r.get("adapter_response") or {}
        if not isinstance(adapter, dict):
            adapter = {}
        node_rows.append(
            (
                invocation_id,
                r.get("unique_id"),
                r.get("status"),
                r.get("execution_time"),
                r.get("message"),
                adapter.get("rows_affected"),
                adapter.get("bytes_processed"),
                adapter.get("bytes_billed"),
            )
        )

    manifest_hash = _store_manifest(conn, manifest) if manifest else None
    cur = conn.execute(
        "INSERT OR IGNORE INTO runs "
        "(invocation_id, generated_at, dbt_version, command, env, status, elapsed, "
        " manifest_hash, run_results_gz, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            invocation_id,
            generated_at,
            meta.get("dbt_version"),
            command,
            env_name,
            status,
            run_results.get("elapsed_time"),
            manifest_hash,
            _gz(run_results),
            _utcnow(),
        ),
    )
    if cur.rowcount == 0:
        conn.commit()
        return IngestResult("duplicate", invocation_id)

    conn.executemany(
        "INSERT OR IGNORE INTO node_results "
        "(invocation_id, unique_id, status, execution_time, message, rows_affected, "
        " bytes_processed, bytes_billed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [row for row in node_rows if row[1]],
    )
    if manifest:
        _refresh_nodes_current(conn, manifest)
    conn.commit()
    return IngestResult("ingested", invocation_id)
