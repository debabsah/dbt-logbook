"""Locate the dbt project, its artifact output, and the history store.

Target path resolution order (highest wins):
    1. explicit --target-path flag
    2. DBT_TARGET_PATH environment variable
    3. target-path in dbt_project.yml
    4. "target"
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


def find_project_dir(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default: cwd) to the dir containing dbt_project.yml."""
    d = (start or Path.cwd()).resolve()
    for candidate in (d, *d.parents):
        if (candidate / "dbt_project.yml").exists():
            return candidate
    return None


def resolve_target_dir(project_dir: Path, flag: str | None = None) -> Path:
    if flag:
        p = Path(flag)
    elif os.environ.get("DBT_TARGET_PATH"):
        p = Path(os.environ["DBT_TARGET_PATH"])
    else:
        target_path = "target"
        try:
            cfg = yaml.safe_load((project_dir / "dbt_project.yml").read_text()) or {}
            target_path = cfg.get("target-path") or "target"
        except (OSError, yaml.YAMLError):
            pass
        p = Path(target_path)
    return p if p.is_absolute() else project_dir / p


def store_path(project_dir: Path) -> Path:
    return project_dir / ".dbtlogbook" / "history.db"


def load_cost_rate(project_dir: Path) -> float | None:
    """Optional cost.rate_per_hour from dbt-logbook.yml - the universal
    duration-based spend estimate. Bytes-based signals work without it."""
    cfg_path = project_dir / "dbt-logbook.yml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        rate = (cfg.get("cost") or {}).get("rate_per_hour")
        return float(rate) if rate is not None else None
    except (OSError, yaml.YAMLError, TypeError, ValueError):
        return None
