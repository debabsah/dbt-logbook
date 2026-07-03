"""v0.5: sources.json ingest, schema v1->v2 migration, freshness + docs serving."""

import json
import sqlite3

from fastapi.testclient import TestClient

from dbt_logbook.api import create_app
from dbt_logbook.ingest import ingest_target_dir
from dbt_logbook.store import _SCHEMA_V1, open_store


def sources_doc(invocation_id: str, snapshotted_at: str, status: str = "pass") -> dict:
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/sources/v3.json",
            "invocation_id": invocation_id,
            "generated_at": snapshotted_at,
        },
        "results": [
            {
                "unique_id": "source.jaffle_shop.raw.orders",
                "status": status,
                "max_loaded_at": "2026-07-01T05:00:00Z",
                "snapshotted_at": snapshotted_at,
            },
            {
                "unique_id": "source.jaffle_shop.raw.payments",
                "status": "pass",
                "max_loaded_at": "2026-07-01T05:30:00Z",
                "snapshotted_at": snapshotted_at,
            },
        ],
    }


def test_sources_only_ingest(store, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "sources.json").write_text(json.dumps(sources_doc("f1", "2026-07-01T06:00:00Z")))
    result = ingest_target_dir(store, target)
    assert result.status == "sources_only"
    assert store.execute("SELECT COUNT(*) FROM source_freshness").fetchone()[0] == 2
    # idempotent
    assert ingest_target_dir(store, target).status == "sources_only"
    assert store.execute("SELECT COUNT(*) FROM source_freshness").fetchone()[0] == 2


def test_sources_ingested_alongside_run(store, target_dir_v1, tmp_path):
    (target_dir_v1 / "sources.json").write_text(
        json.dumps(sources_doc("f2", "2026-07-02T06:00:00Z", status="error"))
    )
    result = ingest_target_dir(store, target_dir_v1)
    assert result.status == "ingested"
    assert store.execute("SELECT COUNT(*) FROM source_freshness").fetchone()[0] == 2


def test_v1_store_migrates_to_v2_with_backup(tmp_path):
    db = tmp_path / "history.db"
    # Build a genuine v1 store by hand (no source_freshness table).
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA_V1)
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
    conn.execute(
        "INSERT INTO runs (invocation_id, status, imported_at) VALUES ('old-run', 'success', 'x')"
    )
    conn.commit()
    conn.close()

    conn = open_store(db)  # migrates v1 -> current
    assert db.with_suffix(".db.bak").exists(), "pre-migration backup missing"
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_freshness").fetchone()[0] == 0
    from dbt_logbook.store import SCHEMA_VERSION

    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert int(version) == SCHEMA_VERSION
    conn.close()


def test_freshness_endpoint_series(store, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    for i, status in enumerate(["pass", "pass", "error"], start=1):
        (target / "sources.json").write_text(
            json.dumps(sources_doc(f"f{i}", f"2026-07-0{i}T06:00:00Z", status=status))
        )
        ingest_target_dir(store, target)

    # store fixture uses its own db file; build an app on the same path
    db_path = store.execute("PRAGMA database_list").fetchone()["file"]
    client = TestClient(create_app(type("P", (), {"__fspath__": lambda s: db_path})()))
    data = client.get("/api/freshness").json()
    orders = next(s for s in data if s["unique_id"].endswith("raw.orders"))
    assert orders["latest_status"] == "error"
    assert [p["status"] for p in orders["snapshots"]] == ["pass", "pass", "error"]


def test_docs_site_mounted_when_index_exists(tmp_path, target_dir_v1):
    db = tmp_path / "history.db"
    conn = open_store(db)
    ingest_target_dir(conn, target_dir_v1)
    conn.close()

    docs = tmp_path / "docs_target"
    docs.mkdir()
    (docs / "index.html").write_text("<html>dbt docs</html>")

    with_docs = TestClient(create_app(db, docs_dir=docs))
    assert with_docs.get("/api/summary").json()["docs_available"] is True
    assert "dbt docs" in with_docs.get("/docs-site/").text

    without = TestClient(create_app(db, docs_dir=tmp_path / "nope"))
    assert without.get("/api/summary").json()["docs_available"] is False
    assert without.get("/docs-site/").status_code == 404
