from fastapi.testclient import TestClient

from dbt_logbook.api import create_app
from dbt_logbook.demo import seed_demo_store
from dbt_logbook.store import open_store


def test_demo_seeds_full_story(tmp_path):
    db = tmp_path / "history.db"
    conn = open_store(db)
    statuses = seed_demo_store(conn, tmp_path / "scratch")
    conn.close()
    assert statuses == ["ingested"] * 5

    client = TestClient(create_app(db))
    runs = client.get("/api/runs").json()
    assert runs["total"] == 5
    assert sum(1 for r in runs["runs"] if r["status"] == "error") == 1

    # The regression is visible: some model's history spans a >4x duration range.
    dag = client.get("/api/dag").json()
    model_ids = [n["id"] for n in dag["nodes"] if n["type"] == "model"]
    spans = []
    for mid in model_ids:
        h = client.get(f"/api/models/{mid}").json()["history"]
        times = [x["execution_time"] for x in h if x["execution_time"]]
        if times:
            spans.append(max(times) / max(min(times), 0.001))
    assert max(spans) > 4, "seeded regression should be visible in some sparkline"

    # The diff screen has content between first and last run.
    ids = [r["invocation_id"] for r in runs["runs"]]
    d = client.get(f"/api/diff?a={ids[-1]}&b={ids[0]}").json()
    assert d["modified"], "seeded checksum change should appear as modified"
