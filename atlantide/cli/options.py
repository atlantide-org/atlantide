"""Option types and prompts shared by more than one command module.

Typer builds a command's interface from its signature, so an option reused across
commands is otherwise re-declared — and drifts. These aliases keep one spelling,
one help string, and one short flag per concept.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, get_args

import typer

from atlantide.cli.errors import fail
from atlantide.reconcile import OnFailure

ConfigArg = Annotated[Path | None, typer.Argument(help="Atlas-lang config (.py).")]
#: The same config, as an option rather than a positional. For commands whose
#: subject is a resource — ``import`` — where the config is context and, in a
#: project with an ``atlantide.toml``, never typed at all.
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
#: No ``ATLANTIDE_ENV`` counterpart: the environment a run acts on is passed on
#: the command line, not inherited from the shell.
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


def stdin_is_tty() -> bool:
    """Whether there is a terminal to prompt on.

    A function rather than an inline check so it can be substituted: a test
    driving the prompt through `CliRunner` supplies stdin as a plain stream,
    which is not a tty, and would otherwise only ever exercise the guard.
    """
    return sys.stdin.isatty()


def require_confirm(confirm: bool, question: str) -> None:
    """Prompt before a mutating action unless ``--confirm`` was passed (aborts on no).

    With no terminal to prompt on, say so instead of prompting. A ``confirm``
    call against a closed stdin aborts with "EOF when reading a line", which
    names the mechanism and not the fix — and it is the *first* thing every user
    hits when they move a working command into CI. The diagnostic below names the
    flag they need.

    Deliberately no ``ATLANTIDE_CONFIRM`` environment variable: a variable that
    silently approves a ``destroy`` for every command in a shell is not a default
    worth introducing, and ``--confirm`` is one flag away.
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

    Values keep the type TOML gave them; a ``-var`` value is a string, because
    that is what a shell hands over and guessing between ``"2"``, ``2`` and
    ``True`` is how a config silently takes the wrong branch. A config wanting a
    number writes ``int(atlantide.input("count"))``.

    Deliberately no ``ATLANTIDE_VAR_*`` environment variable: a value that
    changes the plan should be visible in the command or in a file under review,
    not inherited from whatever the shell happened to export.
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
    """A TOML table of inputs. TOML rather than a bespoke dialect: the project
    already parses it, and it carries types."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        fail(f"cannot read --var-file {path}: {exc.strerror or exc}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"--var-file {path} is not valid TOML: {exc}")


#: The literal values `--on-failure` accepts, derived from the type so the flag
#: and the engine cannot drift.
ON_FAILURE_CHOICES: tuple[str, ...] = get_args(OnFailure)
