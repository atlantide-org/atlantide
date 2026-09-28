"""``atlantide output``: what a previous apply exported.

A separate module because it reads state and evaluates nothing (no config, no
providers, no engine).
"""

from __future__ import annotations

from contextlib import closing
from typing import Annotated, Any

import typer

from atlantide.cli.console import console
from atlantide.cli.context import set_json_mode
from atlantide.cli.errors import fail
from atlantide.cli.options import JsonOpt, StateOpt
from atlantide.cli.target import StateTarget, current_project, resolve_target
from atlantide.cli.views.common import SECRET_REDACTED
from atlantide.cli.views.output import emit_json
from atlantide.secrets import is_sealed_marker

app = typer.Typer()


@app.command()
def output(
    name: Annotated[
        str | None,
        typer.Argument(help="Output name (bare, or `{stack}:{name}`). Omit to list all."),
    ] = None,
    state: StateOpt = None,
    stack: Annotated[
        str | None, typer.Option("--stack", help="Which stack's outputs to read.")
    ] = None,
    json_out: JsonOpt = False,
    reveal: Annotated[
        bool,
        typer.Option("--reveal", "-r", help="Required to print a sensitive value."),
    ] = False,
) -> None:
    """Print the values a previous apply exported with `output()`.

    Reads state only: no config is evaluated and no provider is called, so it
    works while the config is mid-edit or broken.

    With a name, prints the raw value and nothing else, so it pipes:
    `vpc=$(atlantide output vpc_id)`.
    """
    set_json_mode(enabled=json_out)
    project = current_project()
    target = resolve_target(state, project, announce=not (json_out or name))
    with closing(target.open()) as backend:
        stored = backend.outputs()
    sealed = {key for key, value in stored.items() if is_sealed_marker(value)}
    if name is None:
        in_scope = {key for key in sealed if stack is None or key.startswith(f"{stack}:")}
        shown = _unsealed(target, stored, in_scope) if reveal else stored
        _render_outputs(shown, sealed, scope=stack, json_out=json_out, reveal=reveal)
        return
    key = _output_key(stored, name, stack)
    if key in sealed and not reveal:
        fail(f"{key!r} is sensitive — pass --reveal to print it")
    value = _unsealed(target, stored, {key} & sealed)[key]
    if json_out:
        emit_json({"name": key, "value": value, "state": target.label})
        return
    typer.echo(value)


def _unsealed(target: StateTarget, stored: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    """``stored`` with the sealed values in ``keys`` decrypted.

    Unseals only what will be printed: loading the key material fails without a
    keyfile, so a command that prints nothing sealed must not load it.
    """
    if not keys:
        return stored
    secrets = target.secrets()
    return {key: secrets.unseal(value) if key in keys else value for key, value in stored.items()}


def _output_key(resolved: dict[str, Any], name: str, stack: str | None) -> str:
    """Resolve a possibly-bare output name to its stored ``{stack}:{name}`` key.

    A bare name is unambiguous unless two stacks export the same name, which is
    an error rather than a guess.
    """
    if name in resolved:
        return name
    scoped = f"{stack}:{name}" if stack else None
    if scoped is not None:
        if scoped not in resolved:
            fail(f"no output {name!r} in stack {stack!r}")
        return scoped
    matches = [key for key in resolved if key.rsplit(":", 1)[-1] == name]
    if not matches:
        known = ", ".join(sorted(resolved)) or "none recorded"
        fail(f"no output {name!r} — available: {known}")
    if len(matches) > 1:
        fail(
            f"{name!r} is exported by several stacks ({', '.join(sorted(matches))}) "
            f"— pass --stack to choose"
        )
    return matches[0]


def _render_outputs(
    resolved: dict[str, Any],
    sealed: set[str],
    *,
    scope: str | None,
    json_out: bool,
    reveal: bool,
) -> None:
    shown = {
        key: value
        for key, value in sorted(resolved.items())
        if scope is None or key.startswith(f"{scope}:")
    }
    if json_out:
        emit_json(
            {
                "outputs": {
                    key: (SECRET_REDACTED if key in sealed and not reveal else value)
                    for key, value in shown.items()
                }
            }
        )
        return
    if not shown:
        console.print("[dim]no outputs recorded[/]")
        return
    for key, value in shown.items():
        display = SECRET_REDACTED if key in sealed and not reveal else str(value)
        console.print(f"{key} = {display}")
