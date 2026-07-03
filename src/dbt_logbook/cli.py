"""dbt-logbook CLI. Commands land lane by lane; `version` proves the wiring."""

from __future__ import annotations

import typer

from . import __version__

app = typer.Typer(help="Run history for dbt. Every invocation recorded, nothing overwritten.")


@app.command()
def version() -> None:
    """Print the dbt-logbook version."""
    typer.echo(__version__)
