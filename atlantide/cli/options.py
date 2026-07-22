"""Option types and prompts shared by more than one command module.

Typer builds a command's interface from its signature, so an option reused across
commands is otherwise re-declared — and drifts. These aliases keep one spelling,
one help string, and one short flag per concept.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

ConfigArg = Annotated[Path | None, typer.Argument(help="Atlas-lang config (.py).")]
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


def require_confirm(confirm: bool, question: str) -> None:
    """Prompt before a mutating action unless ``--confirm`` was passed (aborts on no)."""
    if not confirm:
        typer.confirm(question, abort=True)
