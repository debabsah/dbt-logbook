"""The platform core: `dbt-logbook serve`.

One long-lived process that:
  1. runs scheduled dbt commands (cron expressions, retries with backoff),
     recording every run through the exec-wrapper path
  2. watches the target dir and auto-imports runs that happened outside the
     wrapper (idempotent ingest makes double-capture a no-op)
  3. sends webhook alerts on state transitions (new failure / recovery)
  4. serves the UI and API

Config lives in dbt-logbook.yml at the project root:

    schedules:
      hourly:
        cron: "0 * * * *"
        command: dbt build
        retries: 2            # optional, default 0
        env: prod             # optional environment label
    notify:
      slack_webhook: https://hooks.slack.com/services/...
      teams_webhook: https://...
      on: [failure, recovery]   # default

Jobs run sequentially - dbt invocations on one project must not overlap.
# Deliberately a single runner thread with a 30s tick; a job queue can come
# if concurrent schedules are ever actually needed.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from croniter import croniter

from .exec_wrapper import run_wrapped
from .paths import store_path
from .store import open_store

TICK_SECONDS = 30
RETRY_BACKOFF_SECONDS = 60


@dataclass
class Schedule:
    name: str
    cron: str
    command: list[str]
    retries: int = 0
    env: str | None = None
    next_fire: datetime | None = None


@dataclass
class NotifyConfig:
    slack_webhook: str | None = None
    teams_webhook: str | None = None
    on: list[str] = field(default_factory=lambda: ["failure", "recovery"])


def load_config(project_dir: Path) -> tuple[list[Schedule], NotifyConfig]:
    cfg_path = project_dir / "dbt-logbook.yml"
    if not cfg_path.exists():
        return [], NotifyConfig()
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    schedules = []
    for name, s in (cfg.get("schedules") or {}).items():
        if not isinstance(s, dict) or not s.get("cron") or not s.get("command"):
            raise ValueError(f"schedule '{name}' needs both 'cron' and 'command'")
        if not croniter.is_valid(s["cron"]):
            raise ValueError(f"schedule '{name}': invalid cron '{s['cron']}'")
        schedules.append(
            Schedule(
                name=name,
                cron=s["cron"],
                command=shlex.split(s["command"]),
                retries=int(s.get("retries", 0)),
                env=s.get("env"),
            )
        )
    n = cfg.get("notify") or {}
    # YAML 1.1 parses the bare key `on:` as boolean True (like yes/off);
    # accept both spellings so users don't need quotes.
    on_value = n.get("on", n.get(True))
    notify = NotifyConfig(
        slack_webhook=n.get("slack_webhook"),
        teams_webhook=n.get("teams_webhook"),
        on=list(on_value or ["failure", "recovery"]),
    )
    return schedules, notify


def alert_decision(prev_status: str | None, new_status: str, notify_on: list[str]) -> str | None:
    """Pure transition logic: returns 'failure', 'recovery', or None."""
    if new_status == "error" and "failure" in notify_on:
        return "failure"
    if new_status == "success" and prev_status == "error" and "recovery" in notify_on:
        return "recovery"
    return None


def build_alert_text(kind: str, schedule_name: str, invocation_id: str | None,
                     detail: str = "") -> str:
    icon = "✗" if kind == "failure" else "✓"
    verb = "failed" if kind == "failure" else "recovered"
    text = f"{icon} dbt-logbook: schedule '{schedule_name}' {verb}"
    if invocation_id:
        text += f" (run {invocation_id})"
    if detail:
        text += f" - {detail}"
    return text


def post_webhook(url: str, text: str, timeout: int = 10) -> bool:
    """Slack and Teams both accept {"text": ...}. Best-effort - never raises."""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


class Runner:
    """Owns the store-adjacent side effects so the loop stays testable."""

    def __init__(self, project_dir: Path, notify: NotifyConfig,
                 sleeper=time.sleep):
        self.project_dir = project_dir
        self.notify = notify
        self.sleeper = sleeper

    def _last_status(self, env: str | None) -> str | None:
        conn = open_store(store_path(self.project_dir))
        try:
            q = "SELECT status FROM runs "
            params: tuple = ()
            if env:
                q += "WHERE env = ? "
                params = (env,)
            row = conn.execute(q + "ORDER BY generated_at DESC LIMIT 1", params).fetchone()
            return row["status"] if row else None
        finally:
            conn.close()

    def run_job(self, sched: Schedule) -> None:
        prev_status = self._last_status(sched.env)
        attempts = sched.retries + 1
        code, result = 1, None
        for attempt in range(1, attempts + 1):
            code, result = run_wrapped(sched.command, self.project_dir, env=sched.env)
            if code == 0:
                break
            if attempt < attempts:
                self.sleeper(RETRY_BACKOFF_SECONDS)
        new_status = "success" if code == 0 else "error"
        kind = alert_decision(prev_status, new_status, self.notify.on)
        if kind:
            text = build_alert_text(
                kind, sched.name,
                result.invocation_id if result else None,
                f"exit {code}" if kind == "failure" else "",
            )
            for url in (self.notify.slack_webhook, self.notify.teams_webhook):
                if url:
                    post_webhook(url, text)


def due_schedules(schedules: list[Schedule], now: datetime) -> list[Schedule]:
    """Initialize/advance next_fire and return schedules due at `now`."""
    due = []
    for s in schedules:
        if s.next_fire is None:
            s.next_fire = croniter(s.cron, now).get_next(datetime)
        if now >= s.next_fire:
            due.append(s)
            s.next_fire = croniter(s.cron, now).get_next(datetime)
    return due


def scheduler_loop(schedules: list[Schedule], runner: Runner,
                   stop: threading.Event) -> None:
    while not stop.is_set():
        for sched in due_schedules(schedules, datetime.now(timezone.utc)):
            runner.run_job(sched)
        stop.wait(TICK_SECONDS)


def watcher_loop(project_dir: Path, target_dir: Path, env: str | None,
                 stop: threading.Event, interval: int = 5) -> None:
    """Poll target/run_results.json mtime; ingest on change. Idempotent
    ingest makes re-seeing a wrapper-captured run a no-op.
    # Plain mtime polling instead of a watcher dependency - 5s latency is fine."""
    from .ingest import ingest_target_dir

    last_mtime = 0.0
    while not stop.is_set():
        rr = target_dir / "run_results.json"
        try:
            mtime = rr.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime > last_mtime:
            last_mtime = mtime
            conn = open_store(store_path(project_dir))
            try:
                ingest_target_dir(conn, target_dir, env=env)
            finally:
                conn.close()
        stop.wait(interval)
