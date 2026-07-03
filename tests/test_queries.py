import json

from dbt_logbook import queries
from dbt_logbook.ingest import ingest_target_dir


def seed(conn, uid_times_status):
    """Insert runs + node_results. uid_times_status: {uid: [(secs, status), ...]}
    Run i gets one row per uid; timestamps ascend with i."""
    n_runs = max(len(v) for v in uid_times_status.values())
    for i in range(n_runs):
        ts = f"2026-06-{i+1:02d}T06:00:00Z"
        run_status = "success"
        rows = []
        for uid, series in uid_times_status.items():
            if i >= len(series):
                continue
            secs, status = series[i]
            if status != "success":
                run_status = "error"
            rows.append((f"run-{i+1}", uid, status, secs, None, None))
        conn.execute(
            "INSERT INTO runs (invocation_id, generated_at, env, status, imported_at) "
            "VALUES (?, ?, 'prod', ?, ?)",
            (f"run-{i+1}", ts, run_status, ts),
        )
        conn.executemany("INSERT INTO node_results VALUES (?,?,?,?,?,?)", rows)
    conn.commit()


def test_find_regressions_flags_slowdown(store):
    seed(store, {
        "model.p.slow": [(1.0, "success")] * 4 + [(5.0, "success")],
        "model.p.steady": [(2.0, "success")] * 5,
        "model.p.fast_noise": [(0.01, "success")] * 4 + [(0.05, "success")],
    })
    out = queries.find_regressions(store, factor=2.0, window=10)
    assert [r["unique_id"] for r in out] == ["model.p.slow"]
    assert out[0]["factor"] == 5.0
    assert out[0]["baseline_median_seconds"] == 1.0


def test_flaky_nodes_counts_flips(store):
    seed(store, {
        "model.p.flaky": [(1, "success"), (1, "error"), (1, "success"), (1, "error"), (1, "success")],
        "model.p.broke_once": [(1, "success"), (1, "success"), (1, "success"), (1, "error"), (1, "error")],
        "model.p.solid": [(1, "success")] * 5,
    })
    out = queries.flaky_nodes(store, window=20, min_flips=2)
    assert [f["unique_id"] for f in out] == ["model.p.flaky"]
    assert out[0]["flips"] == 4
    assert out[0]["currently_failing"] is False


def test_what_broke_newly_broken_detection(store):
    seed(store, {
        "model.p.newly": [(1, "success"), (1, "error")],
        "model.p.always": [(1, "error"), (1, "error")],
        "model.p.fine": [(1, "success"), (1, "success")],
    })
    out = queries.what_broke(store, runs_back=1)
    by_uid = {f["unique_id"]: f for f in out["failures"]}
    assert set(by_uid) == {"model.p.newly", "model.p.always"}
    assert by_uid["model.p.newly"]["newly_broken"] is True
    assert by_uid["model.p.always"]["newly_broken"] is False


def test_run_history_env_filter(store):
    seed(store, {"model.p.a": [(1, "success")] * 3})
    store.execute("UPDATE runs SET env='ci' WHERE invocation_id='run-2'")
    store.commit()
    assert len(queries.run_history(store, env="prod")["runs"]) == 2
    assert len(queries.run_history(store, env="ci")["runs"]) == 1


def test_resolve_node_by_bare_name(store, target_dir_v1):
    ingest_target_dir(store, target_dir_v1)
    uid = queries.resolve_node(store, "customers")
    assert uid == "model.jaffle_shop.customers"
    assert queries.resolve_node(store, uid) == uid
    assert queries.resolve_node(store, "no_such_model") is None


def test_export_last_good_manifest(store, target_dir_v1, tmp_path):
    ingest_target_dir(store, target_dir_v1)
    path = queries.export_last_good_manifest(store, "default", tmp_path / "state")
    assert path is not None and path.name == "manifest.json"
    manifest = json.loads(path.read_text())
    assert manifest["nodes"]
    assert queries.export_last_good_manifest(store, "no_such_env", tmp_path / "s2") is None
