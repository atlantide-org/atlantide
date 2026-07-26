"""``atlantide secret`` — the local encrypted name→value store.

Config references a secret by name; the plaintext lives here and is resolved in
memory at apply. Printing one always costs an explicit ``--reveal``, so a value
cannot reach a terminal, a screen share, or a CI log by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.options import StateOpt
from atlantide.cli.target import StateTarget, load_project
from atlantide.core import AtlantideError
from atlantide.secrets import KeyfileValueStore

app = typer.Typer(help="Manage the local secrets value-store (name → value).")


def _store_for(state: Path | None) -> KeyfileValueStore:
    """The keyfile value-store the ``secret`` subcommands read and write."""
    return StateTarget.resolve(state, load_project()).value_store()


@app.command("set")
def secret_set(
    name: Annotated[str, typer.Argument(help="Secret name, e.g. app/signing-key.")],
    value: Annotated[str | None, typer.Argument(help="Value (prompted if omitted).")] = None,
    state: StateOpt = None,
) -> None:
    """Store a secret value locally (encrypted). Referenced by name via SecretRef."""
    plaintext = value if value is not None else typer.prompt("value", hide_input=True)
    _store_for(state).set(name, plaintext)
    console.print(f"[green]set[/] secret {name!r}")


@app.command("rm")
def secret_rm(
    name: Annotated[str, typer.Argument(help="Secret name to remove.")],
    state: StateOpt = None,
) -> None:
    """Remove a secret from the local store."""
    if _store_for(state).delete(name):
        console.print(f"[green]removed[/] secret {name!r}")
    else:
        fail(f"no such secret {name!r}")


@app.command("get")
def secret_get(
    name: Annotated[str, typer.Argument(help="Secret name to reveal.")],
    reveal: Annotated[
        bool,
        typer.Option("--reveal", "-r", help="Required: confirm you want the plaintext printed."),
    ] = False,
    state: StateOpt = None,
) -> None:
    """Print a secret's plaintext value (guarded by --reveal).

    Writes the raw value to stdout with no decoration so it pipes cleanly. The
    --reveal gate exists so a bare `get` can't accidentally echo a secret into
    terminal scrollback or CI logs.
    """
    if not reveal:
        fail(f"refusing to print {name!r} without --reveal (it exposes the plaintext)")
    try:
        value = _store_for(state).resolve(name)
    except AtlantideError as exc:
        fail(str(exc))
    typer.echo(value)


@app.command("list")
def secret_list(state: StateOpt = None) -> None:
    """List stored secret names (never their values)."""
    names = _store_for(state).names()
    if not names:
        console.print("[dim]no secrets stored[/]")
        return
    for name in names:
        console.print(name)
