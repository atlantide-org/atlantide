"""``atlantide output`` — what a previous apply exported.

Its own module because it is the one command that reads state and evaluates
nothing: no config, no providers, no engine. That is deliberate — it has to keep
working while the config is mid-edit, which is exactly when a script needs the
value it is asking for.
"""

from __future__ import annotations

from contextlib import closing
from typing import Annotated, Any

import typer

from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.json_out import emit_json
from atlantide.cli.options import JsonOpt, StateOpt
from atlantide.cli.render import SECRET_REDACTED
from atlantide.cli.target import load_project
from atlantide.cli.wiring import machine_readable
from atlantide.cli.wiring import target as state_target
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

    Reads state and nothing else — no config is evaluated and no provider is
    called — so it still works when the config is mid-edit or broken, which is
    when a script most needs the value.

    With a name, prints the raw value and nothing else, so it pipes:
    `vpc=$(atlantide output vpc_id)`.
    """
    machine_readable(json_out)
    project = load_project()
    target = state_target(state, project, announce=not (json_out or name))
    with closing(target.open()) as backend:
        stored = backend.outputs()
        secrets = target.secrets()
    resolved = {key: secrets.unseal(value) for key, value in stored.items()}
    sealed = {key for key, value in stored.items() if is_sealed_marker(value)}
    if name is None:
        _render_outputs(resolved, sealed, scope=stack, json_out=json_out, reveal=reveal)
        return
    key = _output_key(resolved, name, stack)
    if key in sealed and not reveal:
        fail(f"{key!r} is sensitive — pass --reveal to print it")
    if json_out:
        emit_json({"name": key, "value": resolved[key], "state": target.label})
        return
    typer.echo(resolved[key])  # raw, undecorated: this is what gets piped


def _output_key(resolved: dict[str, Any], name: str, stack: str | None) -> str:
    """Resolve a possibly-bare output name to its stored ``{stack}:{name}`` key.

    A bare name is the common case and unambiguous in a single-stack project; in
    a multi-stack one it is only ambiguous if two stacks export the same name,
    which is worth an error rather than a guess about which was meant.
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
