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


def _resolve_token(host: str, token: str | None) -> str | None:
    """Non-localhost binds require a token - remote CI runners are the only
    reason to leave localhost, and they authenticate."""
    import os

    token = token or os.environ.get("DBT_LOGBOOK_TOKEN")
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        typer.echo(
            "dbt-logbook: binding beyond localhost requires --token "
            "(or DBT_LOGBOOK_TOKEN).", err=True,
        )
        raise typer.Exit(2)
    return token


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


@app.command()
def ui(
    port: int = typer.Option(8080, help="Port for the local UI."),
    host: str = typer.Option("127.0.0.1", help="Bind address (localhost only by default)."),
    target_path: str = typer.Option(None, help="Override artifact dir for the initial import."),
    env: str = typer.Option(None, help="Environment label for the initial import."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser."),
) -> None:
    """Instant read-only UI: imports current artifacts, then serves the history."""
    import uvicorn

    from .api import create_app
    from .ingest import ingest_target_dir
    from .paths import resolve_target_dir, store_path
    from .store import open_store

    project_dir = _project_dir_or_exit()
    db = store_path(project_dir)
    conn = open_store(db)
    try:
        result = ingest_target_dir(conn, resolve_target_dir(project_dir, target_path), env=env)
    finally:
        conn.close()
    typer.echo(f"dbt-logbook: import {result.status}"
               + (f" ({result.detail})" if result.detail else ""), err=True)

    url = f"http://{host}:{port}"
    typer.echo(f"dbt-logbook: serving {url}", err=True)
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        create_app(db, docs_dir=resolve_target_dir(project_dir, target_path)),
        host=host, port=port, log_level="warning",
    )


@app.command()
def demo(
    port: int = typer.Option(8080, help="Port for the demo UI."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser."),
) -> None:
    """A populated playground: 5 seeded runs with a failure and a regression."""
    import tempfile

    import uvicorn

    from .api import create_app
    from .demo import seed_demo_store
    from .store import open_store

    scratch = Path(tempfile.mkdtemp(prefix="dbt-logbook-demo-"))
    db = scratch / "history.db"
    conn = open_store(db)
    try:
        seed_demo_store(conn, scratch)
    finally:
        conn.close()
    url = f"http://127.0.0.1:{port}"
    typer.echo(f"dbt-logbook demo: 5 runs seeded (1 failure, 1 slowdown) - {url}", err=True)
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(db), host="127.0.0.1", port=port, log_level="warning")


@app.command()
def mcp() -> None:
    """MCP server (stdio) over this project's run history - for Claude/Cursor."""
    from .mcp_server import build_server
    from .paths import store_path

    build_server(store_path(_project_dir_or_exit())).run()


@app.command()
def serve(
    port: int = typer.Option(8080, help="Port for UI + API."),
    host: str = typer.Option("127.0.0.1", help="Bind address (beyond localhost requires --token)."),
    env: str = typer.Option(None, help="Environment label for watcher-imported runs."),
    target_path: str = typer.Option(None, help="Override artifact dir to watch."),
    token: str = typer.Option(None, help="Bearer token required on /api/* (or DBT_LOGBOOK_TOKEN)."),
) -> None:
    """Long-lived platform: scheduler + watcher + alerts + UI. Reads dbt-logbook.yml."""
    import threading

    import uvicorn

    from .api import create_app
    from .paths import resolve_target_dir, store_path
    from .serve import Runner, load_config, scheduler_loop, watcher_loop
    from .store import open_store

    project_dir = _project_dir_or_exit()
    token = _resolve_token(host, token)
    try:
        schedules, notify = load_config(project_dir)
    except ValueError as e:
        typer.echo(f"dbt-logbook: config error: {e}", err=True)
        raise typer.Exit(2)

    db = store_path(project_dir)
    open_store(db).close()  # create/migrate up front
    stop = threading.Event()
    target_dir = resolve_target_dir(project_dir, target_path)
    threads = [
        threading.Thread(
            target=watcher_loop, args=(project_dir, target_dir, env, stop), daemon=True
        )
    ]
    if schedules:
        runner = Runner(project_dir, notify)
        threads.append(
            threading.Thread(
                target=scheduler_loop, args=(schedules, runner, stop), daemon=True
            )
        )
    for t in threads:
        t.start()
    names = ", ".join(s.name for s in schedules) or "none"
    typer.echo(f"dbt-logbook: serving http://{host}:{port} · schedules: {names} "
               f"· watching {target_dir}", err=True)
    try:
        uvicorn.run(
            create_app(db, token=token, docs_dir=target_dir),
            host=host, port=port, log_level="warning",
        )
    finally:
        stop.set()


@app.command()
def state(
    env: str = typer.Option("default", help="Environment whose last-good manifest to export."),
    out: str = typer.Option("dbt-logbook-state", help="Directory to write manifest.json into."),
) -> None:
    """Export the last-good manifest for state-based CI:
    dbt build --select state:modified --defer --state <out>"""
    from .paths import store_path
    from .store import open_store
    from . import queries

    project_dir = _project_dir_or_exit()
    conn = open_store(store_path(project_dir))
    try:
        path = queries.export_last_good_manifest(conn, env, Path(out))
    finally:
        conn.close()
    if path is None:
        typer.echo(f"dbt-logbook: no successful run with a manifest for env '{env}'", err=True)
        raise typer.Exit(1)
    typer.echo(str(path))


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
