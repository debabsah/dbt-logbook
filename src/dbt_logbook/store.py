"""SQLite history store: every dbt run recorded, nothing overwritten.

Schema v1:

    schema_meta      key/value; holds schema_version
    manifest_blobs   gzipped raw manifest.json, keyed by normalized content hash
    runs             one row per dbt invocation (raw run_results kept gzipped)
    node_results     per-node outcome per run
    nodes_current    node index from the most recently ingested manifest

    runs ──< node_results          (invocation_id)
    runs >── manifest_blobs        (manifest_hash)

Concurrency: WAL + busy_timeout. Correctness comes from idempotent ingest
(INSERT OR IGNORE keyed on invocation_id / content hash), not from assuming
a single writer process.

Migrations: forward-only, with a pre-migration file copy - accrued history is
irreplaceable because dbt overwrites its artifacts.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE manifest_blobs (
    hash TEXT PRIMARY KEY,
    gz BLOB NOT NULL,
    node_count INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE runs (
    invocation_id TEXT PRIMARY KEY,
    generated_at TEXT,
    dbt_version TEXT,
    command TEXT,
    env TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL,
    elapsed REAL,
    manifest_hash TEXT REFERENCES manifest_blobs(hash),
    run_results_gz BLOB,
    imported_at TEXT NOT NULL
);
CREATE TABLE node_results (
    invocation_id TEXT NOT NULL REFERENCES runs(invocation_id),
    unique_id TEXT NOT NULL,
    status TEXT,
    execution_time REAL,
    message TEXT,
    rows_affected INTEGER,
    PRIMARY KEY (invocation_id, unique_id)
);
CREATE INDEX idx_node_results_by_node ON node_results (unique_id, invocation_id);
CREATE TABLE nodes_current (
    unique_id TEXT PRIMARY KEY,
    name TEXT,
    resource_type TEXT,
    checksum TEXT,
    depends_on TEXT,
    path TEXT,
    description TEXT
);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
]


def open_store(db_path: Path) -> sqlite3.Connection:
    """Open the history store, creating or migrating it as needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn, db_path)
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if has_meta is None:
        return 0
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def _migrate(conn: sqlite3.Connection, db_path: Path) -> None:
    version = _current_version(conn)
    pending = [(v, sql) for v, sql in MIGRATIONS if v > version]
    if not pending:
        return
    if version > 0 and db_path.exists():
        shutil.copy2(db_path, db_path.with_suffix(".db.bak"))
    for v, sql in pending:
        conn.executescript(sql)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(v),),
        )
        conn.commit()
