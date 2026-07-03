import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from dbt_logbook.store import open_store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(params=["dbt-1.11", "dbt-2.0"], ids=["v1.11", "v2.0"])
def target_dir(request, tmp_path) -> Path:
    """A tmp target/ populated with golden artifacts for one dbt version."""
    src = FIXTURES / request.param
    dst = tmp_path / "target"
    dst.mkdir()
    for f in src.glob("*.json"):
        shutil.copy(f, dst / f.name)
    return dst


@pytest.fixture()
def target_dir_v1(tmp_path) -> Path:
    src = FIXTURES / "dbt-1.11"
    dst = tmp_path / "target"
    dst.mkdir()
    for f in src.glob("*.json"):
        shutil.copy(f, dst / f.name)
    return dst


@pytest.fixture()
def store(tmp_path) -> sqlite3.Connection:
    conn = open_store(tmp_path / ".dbtlogbook" / "history.db")
    yield conn
    conn.close()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())
