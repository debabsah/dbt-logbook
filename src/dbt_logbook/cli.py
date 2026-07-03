"""dbt-logbook CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from . import __version__

app = typer.Typer(help="Run history for dbt. Every invocation recorded, nothing overwritten.")


def _project_dir_or_exit() -> Path:
    from .paths import find_project_dir

    project_dir = find_project_dir()
    if project_dir is None:
        typer.echo("dbt-logbook: no dbt_project.yml found here or above.", err=True)
        raise typer.Exit(2)
    return project_dir


@app.command()
def version() -> None:
    """Print the dbt-logbook version."""
    typer.echo(__version__)


@app.command(
    name="exec",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def exec_cmd(
    ctx: typer.Context,
    env: str = typer.Option(None, help="Environment label for this run (default: dbt target)."),
    target_path: str = typer.Option(None, help="Override artifact dir (default: dbt's)."),
) -> None:
    """Run a dbt command and record it: dbt-logbook exec -- dbt build"""
    cmd = list(ctx.args)
    if not cmd:
        typer.echo("usage: dbt-logbook exec -- dbt build [...]", err=True)
        raise typer.Exit(2)
    if sys.platform == "win32":
        typer.echo("dbt-logbook: exec is unsupported on Windows (see README).", err=True)
        raise typer.Exit(2)

    from .exec_wrapper import run_wrapped

    code, _ = run_wrapped(cmd, _project_dir_or_exit(), target_flag=target_path, env=env)
    raise typer.Exit(code)


@app.command(name="import")
def import_cmd(
    path: str = typer.Argument(None, help="Artifact dir to ingest (default: the project's target path)."),
    env: str = typer.Option(None, help="Environment label, e.g. --env prod for downloaded CI artifacts."),
) -> None:
    """Ingest artifacts into the history store. Safe to run repeatedly."""
    from .ingest import ingest_target_dir
    from .paths import resolve_target_dir, store_path
    from .store import open_store

    project_dir = _project_dir_or_exit()
    target_dir = Path(path) if path else resolve_target_dir(project_dir)
    conn = open_store(store_path(project_dir))
    try:
        result = ingest_target_dir(conn, target_dir, env=env)
    finally:
        conn.close()
    typer.echo(f"dbt-logbook: {result.status}" + (f" ({result.detail})" if result.detail else ""))
    if result.status in ("corrupt",):
        raise typer.Exit(1)
