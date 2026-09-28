"""Option types and prompts shared by more than one command module.

Typer builds a command's interface from its signature; these aliases give each
shared option one spelling, one help string and one short flag.

``--env``, ``--fuel``, ``--confirm`` and ``--var`` have no ``ATLANTIDE_*``
environment-variable counterpart. Each changes what a run does (target
environment, evaluation budget, ``destroy`` approval, plan inputs), so it must come
from the command line or a reviewed file (the checked-in toml, a ``--var-file``),
not the shell. An exported variable would approve a ``destroy`` for every command
in that shell.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, get_args

import typer

from atlantide.cli.errors import fail
from atlantide.cli.project import MAX_FUEL
from atlantide.reconcile import OnFailure

ConfigArg = Annotated[Path | None, typer.Argument(help="Atlas-lang config (.py).")]
#: The config as an option rather than a positional, for commands whose subject
#: is a resource (``import``); with an ``atlantide.toml`` it is usually omitted.
ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Atlas-lang config (.py).")]
StateOpt = Annotated[Path | None, typer.Option("--state", help="State database file.")]
ConfirmOpt = Annotated[
    bool,
    typer.Option("--confirm", "-y", help="Skip the interactive confirmation prompt."),
]
RegionOpt = Annotated[
    str | None, typer.Option("--region", help="AWS region (overrides atlantide.toml).")
]
ParallelismOpt = Annotated[
    int | None,
    typer.Option("--parallelism", "-p", help="Max concurrent provider operations."),
]
JsonOpt = Annotated[
    bool, typer.Option("--json", help="Emit machine-readable JSON instead of text.")
]
VarOpt = Annotated[
    list[str] | None,
    typer.Option("--var", "-var", help="Config input as name=value (repeatable)."),
]
VarFileOpt = Annotated[
    list[Path] | None,
    typer.Option("--var-file", help="TOML file of config inputs (repeatable)."),
]
#: No ``ATLANTIDE_ENV``: see the module docstring.
EnvOpt = Annotated[
    list[str] | None,
    typer.Option("--env", "-e", help="Act only on this Config environment (repeatable)."),
]
TargetOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--target",
        "-t",
        help="Act only on this resource and what it needs (id, short id, or glob).",
    ),
]
ReplaceOpt = Annotated[
    list[str] | None,
    typer.Option("--replace", help="Force this resource to be recreated (repeatable)."),
]
#: No ``ATLANTIDE_FUEL``: see the module docstring.
FuelOpt = Annotated[
    int | None,
    typer.Option(
        "--fuel",
        min=1,
        max=MAX_FUEL,
        help="Atlas-lang evaluation step budget (overrides \\[lang] fuel; default 5,000,000).",
    ),
]


def stdin_is_tty() -> bool:
    """Whether there is a terminal to prompt on.

    A function so tests can substitute it: ``CliRunner`` stdin is never a tty.
    """
    return sys.stdin.isatty()


def require_confirm(question: str, *, confirm: bool) -> None:
    """Prompt before a mutating action unless ``--confirm`` was passed (aborts on no).

    With no terminal, fail with a diagnostic naming the flag instead of prompting;
    ``typer.confirm`` on a closed stdin aborts with only "EOF when reading a line".

    No ``ATLANTIDE_CONFIRM``: see the module docstring.
    """
    if confirm:
        return
    if not stdin_is_tty():
        fail(
            f"cannot ask for confirmation: stdin is not a terminal. Pass --confirm/-y "
            f"to run non-interactively (or --dry-run to see the plan only). Asked: "
            f"{question.strip()}"
        )
    typer.confirm(question, abort=True)


def resolve_inputs(
    project_inputs: Mapping[str, Any],
    var_files: Sequence[Path] | None,
    variables: Sequence[str] | None,
) -> dict[str, Any]:
    """Merge config inputs, most specific last: toml, then files, then flags.

    TOML values keep their type; a ``--var`` value stays a string, since guessing
    between ``"2"``, ``2`` and ``True`` can send a config down the wrong branch. A
    config needing a number writes ``int(atlantide.input("count"))``.

    No ``ATLANTIDE_VAR_*``: see the module docstring.
    """
    merged: dict[str, Any] = dict(project_inputs)
    for path in var_files or ():
        merged.update(_read_var_file(path))
    for entry in variables or ():
        name, separator, value = entry.partition("=")
        if not separator or not name:
            fail(f"--var expects name=value, got {entry!r}")
        merged[name] = value
    return merged


def _read_var_file(path: Path) -> Mapping[str, Any]:
    """Read a ``--var-file``: a TOML table of inputs, with typed values."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        fail(f"cannot read --var-file {path}: {exc.strerror or exc}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"--var-file {path} is not valid TOML: {exc}")


#: The values ``--on-failure`` accepts, derived from ``OnFailure`` so the flag
#: matches the engine.
ON_FAILURE_CHOICES: tuple[str, ...] = get_args(OnFailure)
