"""Wrap a dbt invocation and record it: dbt-logbook exec -- dbt build ...

Guarantees:
- dbt's exit code passes through exactly (cron/CI alerting keeps working)
- SIGINT/SIGTERM are forwarded to dbt
- artifacts are captured even when dbt fails - failed runs are the most
  valuable history
- stale guard: artifacts generated before our start time are never attributed
  to this invocation (a crashed dbt can't inherit the previous run's results)

POSIX only. Windows is documented as unsupported for exec (signal semantics).
"""

from __future__ import annotations

import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .ingest import IngestResult, ingest_target_dir
from .paths import resolve_target_dir, store_path
from .store import open_store


def _utcnow_seconds() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def run_wrapped(
    cmd: list[str],
    project_dir: Path,
    target_flag: str | None = None,
    env: str | None = None,
) -> tuple[int, IngestResult]:
    """Run `cmd`, then ingest whatever it wrote. Returns (exit_code, ingest_result)."""
    started = _utcnow_seconds()
    try:
        proc = subprocess.Popen(cmd)
    except FileNotFoundError:
        print(f"dbt-logbook: command not found: {cmd[0]}", file=sys.stderr)
        return 127, IngestResult("no_artifacts", detail="command not found")

    def _forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    old_int = signal.signal(signal.SIGINT, _forward)
    old_term = signal.signal(signal.SIGTERM, _forward)
    try:
        code = proc.wait()
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    conn = open_store(store_path(project_dir))
    try:
        target_dir = resolve_target_dir(project_dir, target_flag)
        result = ingest_target_dir(conn, target_dir, env=env, not_before=started)
    finally:
        conn.close()
    print(f"dbt-logbook: {result.status}" + (f" ({result.detail})" if result.detail else ""),
          file=sys.stderr)
    return code, result
