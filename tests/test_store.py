import sqlite3

from dbt_logbook import store as store_mod
from dbt_logbook.store import MIGRATIONS, open_store


def test_fresh_store_created_at_current_version(tmp_path):
    db = tmp_path / "history.db"
    conn = open_store(db)
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]
    assert int(version) == store_mod.SCHEMA_VERSION
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"schema_meta", "runs", "node_results", "manifest_blobs", "nodes_current"} <= tables
    conn.close()


def test_reopen_is_noop(tmp_path):
    db = tmp_path / "history.db"
    open_store(db).close()
    conn = open_store(db)
    assert not db.with_suffix(".db.bak").exists()
    conn.close()


def test_wal_mode_enabled(tmp_path):
    conn = open_store(tmp_path / "history.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_forward_migration_backs_up_and_preserves_data(tmp_path, monkeypatch):
    db = tmp_path / "history.db"
    conn = open_store(db)
    conn.execute(
        "INSERT INTO runs (invocation_id, status, imported_at) VALUES ('abc', 'success', 'now')"
    )
    conn.commit()
    conn.close()

    next_version = store_mod.SCHEMA_VERSION + 1
    monkeypatch.setattr(
        store_mod,
        "MIGRATIONS",
        MIGRATIONS + [(next_version, "ALTER TABLE runs ADD COLUMN migrated_col TEXT;")],
    )
    conn = open_store(db)
    assert db.with_suffix(".db.bak").exists(), "pre-migration backup missing"
    row = conn.execute("SELECT invocation_id, migrated_col FROM runs").fetchone()
    assert row["invocation_id"] == "abc"
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]
    assert int(version) == next_version
    conn.close()


def test_concurrent_connections_no_corruption(tmp_path):
    db = tmp_path / "history.db"
    a = open_store(db)
    b = sqlite3.connect(db, timeout=30)
    a.execute(
        "INSERT INTO runs (invocation_id, status, imported_at) VALUES ('x', 'success', 'now')"
    )
    a.commit()
    assert b.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    a.close()
    b.close()
