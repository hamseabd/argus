"""Argus command line."""

import typer

from argus import __version__

app = typer.Typer(
    help="Argus: a code-review agent on the Claude Agent SDK.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Argus command line."""


@app.command()
def version() -> None:
    """Print the Argus version."""
    typer.echo(f"argus {__version__}")
