"""Seed a populated demo store so the first-run UI shows real history.

Uses bundled golden artifacts (jaffle shop, dbt 1.11) rather than running
dbt - dbt-logbook must never depend on a dbt installation. Five runs are
synthesized: run 2 fails one model, runs 4-5 show a duration regression on
another, and run 3 modifies a model so the diff screen has content.
"""

from __future__ import annotations

import copy
import gzip
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

from .ingest import ingest_target_dir

RUNS = 5
FAIL_RUN = 2      # 1-based: this run has one failed model
CHANGE_RUN = 3    # the manifest changes here (diff screen content)
SLOW_FROM_RUN = 4  # duration regression starts here


def _load_bundled(name: str) -> dict:
    data = resources.files("dbt_logbook.demo_data").joinpath(name + ".gz").read_bytes()
    return json.loads(gzip.decompress(data))


def seed_demo_store(conn: sqlite3.Connection, scratch_dir: Path) -> list[str]:
    """Write 5 synthetic runs through the normal ingest path. Returns statuses."""
    manifest = _load_bundled("manifest.json")
    run_results = _load_bundled("run_results.json")
    model_ids = sorted(
        k for k, v in manifest["nodes"].items() if v.get("resource_type") == "model"
    )
    slow_model, fail_model = model_ids[0], model_ids[1 % len(model_ids)]
    target = scratch_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    base = datetime.now(timezone.utc) - timedelta(days=RUNS)

    statuses = []
    for i in range(1, RUNS + 1):
        m = copy.deepcopy(manifest)
        r = copy.deepcopy(run_results)
        ts = (base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for doc in (m, r):
            doc["metadata"]["invocation_id"] = f"demo-run-{i}"
            doc["metadata"]["generated_at"] = ts
        if i >= CHANGE_RUN:
            m["nodes"][slow_model]["checksum"]["checksum"] = "demo-changed-checksum"
        for res in r["results"]:
            if res["unique_id"] == slow_model and i >= SLOW_FROM_RUN:
                res["execution_time"] = 0.4 + 2.1 * (i - SLOW_FROM_RUN + 1)
            if res["unique_id"] == fail_model and i == FAIL_RUN:
                res["status"] = "error"
                res["message"] = "Database Error: relation does not exist (demo)"
        (target / "manifest.json").write_text(json.dumps(m))
        (target / "run_results.json").write_text(json.dumps(r))
        statuses.append(ingest_target_dir(conn, target).status)
    return statuses
