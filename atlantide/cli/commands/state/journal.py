"""``atlantide state compact`` / ``fsck`` — maintain the S3 state journal.

Only the S3 backend keeps a journal.
"""

from __future__ import annotations

import time
from contextlib import closing
from typing import TYPE_CHECKING, Annotated

import typer

from atlantide.cli.commands.state.common import announced_target
from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.options import ConfirmOpt, require_confirm
from atlantide.cli.views.state import render_fsck
from atlantide.state import StateBackend

if TYPE_CHECKING:
    from atlantide.state.s3 import S3StateBackend

__all__ = ["compact", "fsck"]

RebuildHeadsOpt = Annotated[
    bool,
    typer.Option(
        "--rebuild-heads",
        help="Re-point lost journal heads at their highest-seq entry (review each one).",
    ),
]


def _journal(backend: StateBackend) -> S3StateBackend:
    """Return ``backend`` as the S3 journal backend; fail for any other backend."""
    # Deferred import: the S3 backend imports boto3, which other commands and
    # `--help` do not need.
    from atlantide.state.s3 import S3StateBackend

    if not isinstance(backend, S3StateBackend):
        fail("only the s3 state backend keeps a journal — there is nothing to do here")
    return backend


def compact() -> None:
    """Fold the S3 state journal into its snapshot and delete what that supersedes.

    Compaction runs automatically after every locked run and in the background
    during long ones; run this after crashed runs, or before taking the lock table
    down for maintenance. It takes no lock and loses no write: commits made while
    it runs stay in the journal.
    """
    target = announced_target()
    with closing(target.open()) as backend:
        report = _journal(backend).compact()
    if report.skipped:
        console.print("[yellow]skipped[/]: another compaction or bulk write got there first")
        return
    console.print(
        f"[green]compacted[/] {report.folded} journal head(s) into the snapshot "
        f"(serial {report.serial}, generation {report.gen}); deleted "
        f"{report.deleted} superseded entr{'y' if report.deleted == 1 else 'ies'}"
    )


def fsck(
    rebuild_heads: RebuildHeadsOpt = False,
    confirm: ConfirmOpt = False,
) -> None:
    """Cross-check the S3 state journal: every head's entry exists, no head is lost.

    A head whose entry is missing is corruption (restore the object from bucket
    versioning, or restore a backup). Entries past the snapshot with no head
    mean the heads table lost them, as when the lock table is recreated. ``--rebuild-heads``
    points each such head at its highest-seq entry; that entry may be a write
    that never committed, so every rebuilt node is listed for review. It refuses
    while any run holds a lock on this state.
    """
    target = announced_target()
    with closing(target.open()) as backend:
        journal = _journal(backend)
        if rebuild_heads:
            live = {nid for nid, lease in backend.locks().items() if lease.expires_at > time.time()}
            if live:
                fail(
                    f"{len(live)} node(s) are locked by a live run — rebuilding heads "
                    f"under a writer would race it; wait, or `state unlock` a dead run"
                )
            require_confirm(
                "Rebuild lost journal heads from their newest entries?", confirm=confirm
            )
        report = journal.fsck(rebuild_heads=rebuild_heads)
    if render_fsck(report):
        raise typer.Exit(1)
