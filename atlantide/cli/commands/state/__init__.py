"""``atlantide state`` — administer the state backend itself.

Commands about the store rather than the resources in it: ``check`` verifies the
backend is reachable and safely configured, ``backup``/``restore`` snapshot it
and put a snapshot back, ``list``/``show``/``rm`` read and prune what it records,
``migrate`` copies state between the local database and the remote backend,
``unlock`` shows and breaks leases a dead run left behind, and
``compact``/``fsck`` maintain the S3 state journal.

They share one shape: resolve the target, open exactly one backend, close it.
:class:`~atlantide.cli.target.StateTarget` supplies the first part; ``closing``
the last.
"""

from __future__ import annotations

import typer

from atlantide.cli.commands.state.check import check
from atlantide.cli.commands.state.journal import compact, fsck
from atlantide.cli.commands.state.locks import unlock
from atlantide.cli.commands.state.migrate import migrate
from atlantide.cli.commands.state.nodes import list_nodes, remove_nodes, show_node
from atlantide.cli.commands.state.snapshot import backup, restore

__all__ = ["app"]

app = typer.Typer(help="Inspect, move, and unblock engine state.")

# Registration order is `--help` order.
app.command("check")(check)
app.command("backup")(backup)
app.command("restore")(restore)
app.command("list")(list_nodes)
app.command("show")(show_node)
app.command("rm")(remove_nodes)
app.command("migrate")(migrate)
app.command("unlock")(unlock)
app.command("compact")(compact)
app.command("fsck")(fsck)
