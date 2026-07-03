"""PR report: what this change does, judged against recorded history.

Read-only. Compares the CI run's fresh artifacts (target/, produced by
`dbt build --defer --state ...` on the PR branch) against the last-good
manifest and per-model history for an environment - from the local store, or
from a remote dbt-logbook server (--server/--token) since CI checkouts have
no local history.

Output is markdown for a PR comment; exit-code policy belongs to the caller.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import queries
from .ingest import _FAILURE_STATUSES, _read_json

REGRESSION_FACTOR = 2.0
MIN_SECONDS = 1.0
MARKER = "<!-- dbt-logbook-ci-report -->"


# ---------- history providers ----------

class LocalHistory:
    def __init__(self, conn: sqlite3.Connection, env: str):
        self.conn, self.env = conn, env

    def state_checksums(self) -> dict | None:
        row = self.conn.execute(
            "SELECT manifest_hash FROM runs WHERE env = ? AND status = 'success' "
            "AND manifest_hash IS NOT NULL ORDER BY generated_at DESC LIMIT 1",
            (self.env,),
        ).fetchone()
        if row is None:
            return None
        manifest = queries.load_manifest(self.conn, row["manifest_hash"])
        return _node_checksums(manifest) if manifest else None

    def model_median_seconds(self, unique_id: str) -> float | None:
        rows = self.conn.execute(
            "SELECT execution_time FROM node_results WHERE unique_id = ? "
            "AND execution_time IS NOT NULL ORDER BY rowid DESC LIMIT 10",
            (unique_id,),
        ).fetchall()
        times = [r["execution_time"] for r in rows]
        return statistics.median(times) if times else None


class RemoteHistory:
    """Thin client over the dbt-logbook server API (see docs/api-contract.md)."""

    def __init__(self, server: str, env: str, token: str | None = None):
        self.server, self.env, self.token = server.rstrip("/"), env, token

    def _get(self, path: str):
        req = urllib.request.Request(self.server + path)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def state_checksums(self) -> dict | None:
        manifest = self._get(f"/api/state/{self.env}/manifest.json")
        return _node_checksums(manifest) if manifest else None

    def model_median_seconds(self, unique_id: str) -> float | None:
        d = self._get(f"/api/models/{unique_id}")
        if not d:
            return None
        times = [h["execution_time"] for h in d.get("history") or [] if h.get("execution_time")]
        return statistics.median(times[-10:]) if times else None


def _node_checksums(manifest: dict) -> dict:
    out = {}
    for uid, node in (manifest.get("nodes") or {}).items():
        if isinstance(node, dict):
            cs = node.get("checksum") or {}
            out[uid] = cs.get("checksum") if isinstance(cs, dict) else None
    return out


# ---------- report ----------

@dataclass
class Report:
    state_found: bool = True
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    impacted: list = field(default_factory=list)      # downstream of changed
    failures: list = field(default_factory=list)      # dicts
    regressions: list = field(default_factory=list)   # dicts
    detail: str = ""

    @property
    def verdict(self) -> str:
        if not self.state_found:
            return "no-state"
        if self.failures:
            return "failing"
        if self.regressions:
            return "regressions"
        return "clean"


def build_report(target_dir: Path, history, cost_rate: float | None = None) -> Report:
    manifest = _read_json(target_dir / "manifest.json")
    run_results = _read_json(target_dir / "run_results.json")
    if manifest is None:
        return Report(state_found=False, detail="no manifest.json in target dir - run dbt first")

    r = Report()
    baseline = history.state_checksums()
    current = _node_checksums(manifest)
    if baseline is None:
        r.state_found = False
        r.detail = "no last-good state recorded for this environment"
    else:
        r.added = sorted(set(current) - set(baseline))
        r.removed = sorted(set(baseline) - set(current))
        r.modified = sorted(
            uid for uid in set(current) & set(baseline) if current[uid] != baseline[uid]
        )
        children: dict[str, list[str]] = {}
        for uid, node in (manifest.get("nodes") or {}).items():
            if isinstance(node, dict):
                deps = (node.get("depends_on") or {}).get("nodes") or []
                for d in deps:
                    children.setdefault(d, []).append(uid)
        changed = set(r.added) | set(r.modified)
        seen = set(changed)
        frontier = set(changed)
        while frontier:
            nxt = set()
            for uid in frontier:
                nxt.update(children.get(uid, []))
            nxt -= seen
            seen |= nxt
            frontier = nxt
        r.impacted = sorted(seen - changed)

    for res in (run_results or {}).get("results") or []:
        if not isinstance(res, dict):
            continue
        uid = res.get("unique_id")
        if str(res.get("status")).lower() in _FAILURE_STATUSES:
            r.failures.append({"unique_id": uid, "message": res.get("message")})
            continue
        secs = res.get("execution_time")
        if uid and secs and secs >= MIN_SECONDS:
            median = history.model_median_seconds(uid)
            if median and median > 0 and secs / median >= REGRESSION_FACTOR:
                entry = {
                    "unique_id": uid,
                    "seconds": round(secs, 2),
                    "median": round(median, 2),
                    "factor": round(secs / median, 1),
                }
                if cost_rate:
                    entry["extra_cost_per_run"] = round((secs - median) / 3600 * cost_rate, 2)
                r.regressions.append(entry)
    r.regressions.sort(key=lambda x: -x["factor"])
    return r


def to_markdown(r: Report, env: str) -> str:
    icon = {"clean": "✓", "failing": "✗", "regressions": "⚠", "no-state": "•"}[r.verdict]
    title = {
        "clean": "looks safe to merge",
        "failing": f"{len(r.failures)} failing node(s)",
        "regressions": f"{len(r.regressions)} duration regression(s)",
        "no-state": "no baseline state - showing run results only",
    }[r.verdict]
    lines = [MARKER, f"### {icon} dbt-logbook: {title}", ""]
    if r.detail:
        lines += [f"_{r.detail}_", ""]
    if r.state_found:
        name = lambda uid: uid.split(".")[-1]
        lines += [
            f"**Changed vs last good `{env}`:** "
            f"{len(r.modified)} modified, {len(r.added)} added, {len(r.removed)} removed"
            + (f" · **{len(r.impacted)} downstream impacted**" if r.impacted else ""),
            "",
        ]
        if r.modified:
            names = ", ".join("`" + name(u) + "`" for u in r.modified[:15])
            extra = f" (+{len(r.modified) - 15} more)" if len(r.modified) > 15 else ""
            lines += [f"**Modified:** {names}{extra}", ""]
    if r.failures:
        lines += ["**Failures:**", ""]
        for f in r.failures[:10]:
            msg = (f.get("message") or "").splitlines()[0][:120]
            lines.append(f"- ✗ `{f['unique_id']}` - {msg}")
        lines.append("")
    if r.regressions:
        lines += ["**Slower than their history:**", ""]
        for g in r.regressions[:10]:
            cost = f" (~+${g['extra_cost_per_run']}/run)" if g.get("extra_cost_per_run") else ""
            lines.append(
                f"- ⚠ `{g['unique_id'].split('.')[-1]}`: {g['seconds']}s vs median {g['median']}s "
                f"({g['factor']}x){cost}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
