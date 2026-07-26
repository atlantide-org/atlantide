"""``atlantide state`` — administer the state backend itself.

Commands about the store rather than the resources in it: ``check`` verifies the
backend is reachable and safely configured, ``backup``/``restore`` snapshot it
and put a snapshot back, ``migrate`` copies state between the local database and
the remote backend, and ``unlock`` shows and breaks leases a dead run left behind.

They share one shape: resolve the target, open exactly one backend, close it.
:class:`~atlantide.cli.target.StateTarget` supplies the first part; ``closing``
the last.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from atlantide.cli.console import console
from atlantide.cli.context import set_json_mode
from atlantide.cli.errors import fail
from atlantide.cli.json_out import emit_json
from atlantide.cli.options import ConfirmOpt, JsonOpt, StateOpt, require_confirm
from atlantide.cli.render import SECRET_REDACTED
from atlantide.cli.target import StateTarget, default_state, load_project
from atlantide.core import AtlantideError
from atlantide.core.check import FAIL, OK, SKIP, WARN, Check, Status
from atlantide.core.errors import StateError
from atlantide.engine.locking import held_lock
from atlantide.secrets import is_sealed_marker
from atlantide.state import SqliteStateBackend
from atlantide.state.backend import (
    NO_INPUT_HASH,
    STATUS_CREATED,
    Lease,
    LockPolicy,
    StateBackend,
    StateNode,
)
from atlantide.state.codec import StateDocument, decode, encode

app = typer.Typer(help="Inspect, move, and unblock engine state.")

#: How each status renders; the colour carries severity at a glance.
_MARK: dict[Status, str] = {
    OK: "[green]ok  [/]",
    WARN: "[yellow]warn[/]",
    FAIL: "[red]fail[/]",
    SKIP: "[dim]--  [/]",
}


def _announced_target(state: Path | None = None) -> StateTarget:
    """This invocation's state target, having said which one it is."""
    set_json_mode(False)
    target = StateTarget.resolve(state, load_project())
    target.announce()
    return target


def _quiet_target(state: Path | None = None) -> StateTarget:
    """The target without the banner — for `--json`, where the same value rides
    along as a field rather than corrupting the document with a stray line.

    Declaring the mode here is what routes a warning or error raised later to
    stderr rather than into the middle of the payload. Both this and
    :func:`_announced_target` state their mode explicitly: these subcommands
    choose it per command rather than inheriting the root flag, so saying so is
    the point, not a defence against a leftover value.
    """
    set_json_mode(True)
    return StateTarget.resolve(state, load_project())


# -- check --------------------------------------------------------------------


@app.command("check")
def check(
    probe: Annotated[
        bool,
        typer.Option(
            "--probe/--no-probe",
            help="Also verify conditional writes by writing to a scratch key.",
        ),
    ] = True,
) -> None:
    """Verify the configured state backend is reachable and safely set up.

    The bucket, the lock table and their settings are the trust root for shared
    state, and atlantide deliberately does not create them. Nothing else verifies
    them either: versioning left off or a missing lock-table TTL costs nothing
    until the day it costs everything. This reports all of it at once, rather
    than letting an apply discover it one failed call at a time.
    """
    target = _announced_target()
    with closing(target.open()) as backend:
        checks = backend.check()
        if probe:
            checks.append(backend.probe())
    checks.append(_secrets_check(target))
    for result in checks:
        # Escaped: details quote config keys such as [state].backend.
        console.print(f"{_MARK[result.status]} {result.name}: {escape(result.detail)}")
    if any(result.failed for result in checks):
        raise typer.Exit(1)


def _secrets_check(target: StateTarget) -> Check:
    """The configured provider's own verdict on whether it can serve a secret.

    Building the registry is itself part of what is being checked — an unknown
    AWS profile or an unreadable keyfile fails here rather than in ``check()`` —
    so both steps are guarded. A doctor command reports what is wrong; it does
    not become one more thing that crashes.
    """
    name = target.project.secrets.provider
    try:
        return target.secrets().get(name).check()
    except AtlantideError as exc:
        return Check(f"secrets: {name}", FAIL, str(exc))
    except Exception as exc:
        # Broad on purpose: a provider's SDK raises on its own terms (botocore
        # ProfileNotFound, an OS error on the keyfile) and none of it should abort
        # the report.
        return Check(f"secrets: {name}", FAIL, f"{type(exc).__name__}: {exc}")


# -- backup / restore ---------------------------------------------------------
#
# The snapshot format is the document encoding the S3 backend already uses:
# canonical JSON, gzipped past a threshold, self-describing and version-checked
# on read. A second format would be a second compatibility surface.

#: Extension for a state snapshot. Distinct from `.atlas` (a compiled config);
#: both are content-addressed blobs and easily confused.
SNAPSHOT_SUFFIX = ".atlas-state"


def _default_snapshot(serial: int) -> Path:
    """A snapshot name carrying the serial it was taken at, so a directory of
    them sorts into the order they were taken and names its own provenance."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return Path(f"atlantide-state-{serial}-{stamp}{SNAPSHOT_SUFFIX}")


@app.command("backup")
def backup(
    path: Annotated[
        Path | None,
        typer.Argument(
            help=f"Where to write the snapshot (default: ./atlantide-state-*{SNAPSHOT_SUFFIX})."
        ),
    ] = None,
    state: StateOpt = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing file at that path.")
    ] = False,
) -> None:
    """Write the whole of state — nodes, outputs, serial — to one file.

    Take one before anything that rewrites state in bulk: an upgrade that
    migrates the schema, a `state restore`, a `--force` migration. Recovery
    otherwise depends on the store's own history, which for the local database
    is nothing at all and for S3 is whatever bucket versioning was left set to.

    Held under the state lock, because the table-shaped backends read node by
    node: a snapshot taken while an apply is writing would capture some of that
    apply's nodes and not others, and nothing about the file would say so.
    """
    target = _announced_target(state)
    with closing(target.open()) as backend:
        scope = frozenset(backend.load().nodes)
        with held_lock(
            backend,
            scope,
            policy=target.project.state_backend.lock_policy(),
        ):
            graph = backend.load()
            # Re-checked after acquiring, as destroy() does: the scope was
            # computed before the lease, so a node id created while this
            # command waited for the lock is not covered by it — an apply
            # writing that node could tear the snapshot around it.
            created = set(graph.nodes) - scope
            if created:
                fail(
                    "state gained node(s) while backup waited for the lock: "
                    + ", ".join(sorted(created))
                    + " — re-run backup"
                )
            doc = StateDocument(
                serial=backend.serial(),
                nodes=dict(graph.nodes),
                outputs=backend.outputs(),
            )
        destination = path if path is not None else _default_snapshot(doc.serial)
        if destination.exists() and not force:
            fail(f"{destination} already exists — pass --force to overwrite it")
        destination.write_bytes(encode(doc))
    console.print(
        f"[green]backed up[/] {len(doc.nodes)} node(s) at serial {doc.serial} "
        f"to {escape(str(destination))}"
    )


@app.command("restore")
def restore(
    path: Annotated[Path, typer.Argument(help="Snapshot written by `state backup`.")],
    state: StateOpt = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Restore even though state has changed since the snapshot was taken.",
        ),
    ] = False,
    confirm: ConfirmOpt = False,
) -> None:
    """Replace the contents of state with a snapshot.

    This does not touch a single cloud resource — it rewrites atlantide's record
    of them. Restoring an old snapshot therefore *creates* drift rather than
    undoing it: resources created after the snapshot become untracked, and the
    next apply will try to create them again. Use it to undo a bad state write,
    not a bad deployment.

    Refuses when state has moved since the snapshot was taken unless --force,
    since that is exactly the case where the two disagree about live resources.
    """
    if not path.is_file():
        fail(f"no snapshot at {path}")
    doc = _read_snapshot(path)
    target = _announced_target(state)
    with closing(target.open()) as backend:
        current_serial = backend.serial()
        if current_serial != doc.serial and not force:
            fail(
                f"state is at serial {current_serial} but this snapshot was taken at "
                f"{doc.serial} — it has been written to since. Pass --force to replace "
                f"it anyway, after checking what changed"
            )
        current = backend.load()
        obsolete = sorted(set(current.nodes) - set(doc.nodes))
        added = sorted(set(doc.nodes) - set(current.nodes))
        _render_restore(doc, added, obsolete)
        require_confirm(confirm, f"\nReplace state with {len(doc.nodes)} node(s) from {path}?")
        with held_lock(
            backend,
            frozenset(current.nodes) | frozenset(doc.nodes),
            policy=target.project.state_backend.lock_policy(),
        ):
            backend.replace_many(obsolete, doc.nodes.values())
            backend.set_outputs(
                doc.outputs, remove=sorted(set(backend.outputs()) - set(doc.outputs))
            )
    console.print(f"[green]restored[/] {len(doc.nodes)} node(s) from {escape(str(path))}")


def _read_snapshot(path: Path) -> StateDocument:
    """Decode a snapshot, reporting a bad file as a diagnostic rather than a trace."""
    try:
        return decode(path.read_bytes())
    except (StateError, OSError) as exc:
        fail(f"cannot read snapshot {path}: {exc}")


def _render_restore(doc: StateDocument, added: list[str], obsolete: list[str]) -> None:
    """What the restore will change, before asking. Nodes present in both are
    overwritten with the snapshot's copy and are not listed — the interesting
    cases are the ones that appear or disappear."""
    console.print(f"restoring {len(doc.nodes)} node(s) taken at serial {doc.serial}")
    for node_id in added:
        console.print(f"  [green]+ restored[/] {escape(node_id)}")
    for node_id in obsolete:
        console.print(
            f"  [red]- forgotten[/] {escape(node_id)} "
            f"[dim](not in the snapshot; the resource is not destroyed)[/]"
        )


# -- list / show / rm ---------------------------------------------------------
#
# What state *contains*, as opposed to which backend it lives in. Reads take no
# lock: a locked read would block on a wedged apply, which is exactly when
# someone needs to look, and a torn listing is harmless since nothing acts on it.


@app.command("list")
def list_nodes(
    state: StateOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """List the resources state records.

    A row marked DRIFTED has no usable input hash, so the next plan cannot skip
    it. That is set deliberately — by ``refresh --write`` when it sees live drift,
    and by a failed rollback — and it is the channel through which a state row
    that stopped describing reality becomes visible.
    """
    target = _quiet_target(state) if json_out else _announced_target(state)
    with closing(target.open()) as backend:
        nodes = backend.load().nodes
    if json_out:
        emit_json(
            {
                "state": target.label,
                "nodes": [
                    {
                        "node_id": node.id,
                        "type": node.type,
                        "provider": node.provider,
                        "status": node.status,
                        "drifted": node.input_hash == NO_INPUT_HASH,
                        "depends_on": list(node.dependencies),
                    }
                    for _, node in sorted(nodes.items())
                ],
            }
        )
        return
    if not nodes:
        console.print("[dim]state is empty[/]")
        return
    table = Table(title=f"{len(nodes)} resource(s)")
    table.add_column("node", style="bold")
    table.add_column("type")
    table.add_column("status")
    for node_id, node in sorted(nodes.items()):
        table.add_row(node_id, node.type, _status_of(node))
    console.print(table)


def _status_of(node: StateNode) -> str:
    """The row's condition, worst-first. Anything but `created` is worth seeing."""
    if node.input_hash == NO_INPUT_HASH:
        return "[red]DRIFTED[/]"
    if node.status != STATUS_CREATED:
        return f"[yellow]{node.status}[/]"
    return "[dim]created[/]"


@app.command("show")
def show_node(
    node_id: Annotated[str, typer.Argument(help="Node id, as `state list` prints it.")],
    state: StateOpt = None,
    json_out: JsonOpt = False,
    reveal: Annotated[
        bool,
        typer.Option("--reveal", "-r", help="Print sealed output values in the clear."),
    ] = False,
) -> None:
    """Print everything state records about one resource.

    Sensitive outputs are sealed at rest and stay redacted without ``--reveal``,
    for the same reason ``secret get`` requires it: this lands in terminal
    scrollback and CI logs, which outlive the command.
    """
    target = _quiet_target(state) if json_out else _announced_target(state)
    with closing(target.open()) as backend:
        node = backend.load().get(node_id)
        if node is None:
            fail(f"no node {node_id!r} in state — `atlantide state list` shows what is there")
        outputs = _shown_outputs(node, target, reveal=reveal)
    if json_out:
        emit_json(
            {
                "state": target.label,
                "node_id": node.id,
                "type": node.type,
                "provider": node.provider,
                "provider_version": node.provider_version,
                "status": node.status,
                "drifted": node.input_hash == NO_INPUT_HASH,
                "prevent_destroy": node.prevent_destroy,
                "depends_on": list(node.dependencies),
                "properties": node.properties,
                "outputs": outputs,
            }
        )
        return
    console.print(f"[bold]{escape(node.id)}[/]")
    console.print(f"  type       {escape(node.type)}")
    console.print(f"  provider   {escape(node.provider)} {escape(node.provider_version)}")
    console.print(f"  status     {_status_of(node)}")
    if node.prevent_destroy:
        console.print("  [yellow]prevent_destroy[/]")
    if node.dependencies:
        console.print(f"  depends on {escape(', '.join(node.dependencies))}")
    _print_mapping("Inputs", node.properties)
    _print_mapping("Outputs", outputs)


def _shown_outputs(node: StateNode, target: StateTarget, *, reveal: bool) -> dict[str, Any]:
    """The node's outputs, unsealed only when asked for."""
    if not reveal:
        return {
            key: SECRET_REDACTED if is_sealed_marker(value) else value
            for key, value in node.outputs.items()
        }
    secrets = target.secrets()
    return {key: secrets.unseal(value) for key, value in node.outputs.items()}


def _print_mapping(title: str, values: Mapping[str, Any]) -> None:
    if not values:
        return
    console.print(f"  [bold]{title}:[/]")
    for key, value in sorted(values.items()):
        console.print(f"    {escape(key)} = {escape(str(value))}")


@app.command("rm")
def remove_nodes(
    node_ids: Annotated[list[str], typer.Argument(help="Node ids to forget.")],
    state: StateOpt = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Also forget nodes marked prevent_destroy."),
    ] = False,
    backup: Annotated[
        bool,
        typer.Option("--backup/--no-backup", help="Snapshot state first (default: yes)."),
    ] = True,
    confirm: ConfirmOpt = False,
) -> None:
    """Forget a resource without destroying it.

    The escape hatch for a state row that has stopped describing anything real —
    a resource deleted out of band, or one left behind by a failed rollback.

    It does not touch the provider. If the resource *does* still exist, atlantide
    stops tracking it and the next apply will try to create a second one; use
    `atlantide destroy` if the intent is to remove the resource itself.
    """
    target = _announced_target(state)
    with closing(target.open()) as backend:
        nodes = backend.load().nodes
        if missing := sorted(set(node_ids) - set(nodes)):
            fail(f"not in state: {', '.join(missing)}")
        protected = sorted(nid for nid in node_ids if nodes[nid].prevent_destroy)
        if protected and not force:
            fail(
                f"{', '.join(protected)} declare prevent_destroy — pass --force to "
                f"forget them anyway"
            )
        for node_id in sorted(set(node_ids)):
            console.print(f"  [red]- forget[/] {escape(node_id)} [dim]({nodes[node_id].type})[/]")
        console.print(
            "\n[yellow]The underlying resources are not destroyed.[/] They stay live "
            "and untracked, and the next apply will try to create them again."
        )
        require_confirm(confirm, f"\nForget {len(set(node_ids))} node(s)?")
        if backup:
            snapshot = _default_snapshot(backend.serial())
            snapshot.write_bytes(
                encode(
                    StateDocument(
                        serial=backend.serial(),
                        nodes=dict(nodes),
                        outputs=backend.outputs(),
                    )
                )
            )
            console.print(f"[dim]backed up to {escape(str(snapshot))}[/]")
        with held_lock(
            backend,
            frozenset(node_ids),
            policy=target.project.state_backend.lock_policy(),
        ):
            for node_id in sorted(set(node_ids)):
                backend.delete(node_id)
    console.print(f"[green]forgot[/] {len(set(node_ids))} node(s)")


# -- migrate ------------------------------------------------------------------


@app.command("migrate")
def migrate(
    source: Annotated[
        Path | None, typer.Option("--from", help="Local state database to copy from.")
    ] = None,
    to_local: Annotated[
        Path | None,
        typer.Option(
            "--to-local",
            help="Reverse direction: copy the remote backend into this local database.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite a destination that already holds state."),
    ] = False,
    confirm: ConfirmOpt = False,
) -> None:
    """Copy state between the local database and the remote backend.

    Adopting a backend runs one way by default (local -> the ``[state]`` table);
    ``--to-local`` runs the other, so leaving a shared backend is as easy as
    joining one. Either direction refuses a destination that already holds nodes
    unless ``--force`` is given: merging two states is a decision, not a guess.
    """
    project = load_project()
    if not project.state_backend.is_remote:
        fail("no remote backend configured — set [state].backend in atlantide.toml")
    remote = StateTarget.resolve(None, project)
    copy = (
        _adopt_local(remote, to_local)
        if to_local is not None
        else _adopt_remote(remote, source if source is not None else default_state(project))
    )
    _run(copy, force=force, confirm=confirm, policy=project.state_backend.lock_policy())


@dataclass(frozen=True, slots=True)
class _Copy:
    """One direction of a migration: two open backends and what to say afterwards."""

    source: StateBackend
    source_label: str
    destination: StateBackend
    destination_label: str
    #: What the operator still has to do once the bytes have moved.
    epilogue: str


def _adopt_remote(remote: StateTarget, source: Path) -> _Copy:
    if not source.is_file():
        fail(f"no local state database at {source}")
    return _Copy(
        source=SqliteStateBackend(str(source)),
        source_label=str(source),
        destination=remote.open(),
        destination_label=remote.label,
        epilogue=(
            f"{source} is no longer read — keep it as a backup or remove it, "
            f"but do not keep applying against both"
        ),
    )


def _adopt_local(remote: StateTarget, destination: Path) -> _Copy:
    return _Copy(
        source=remote.open(),
        source_label=remote.label,
        destination=SqliteStateBackend(str(destination)),
        destination_label=str(destination),
        epilogue=(
            f"remove the [state] table from atlantide.toml (or pass "
            f"--state {destination}) for commands to use it"
        ),
    )


def _run(copy: _Copy, *, force: bool, confirm: bool, policy: LockPolicy) -> None:
    """Move a whole state across, in whichever direction ``copy`` describes.

    One write (``put_many``), not a loop: a migration interrupted halfway would
    leave a destination that is neither empty — so a retry refuses it — nor
    complete, so an apply would recreate live resources.

    Both sides are locked for the copy. Without that, an apply running against
    the source mid-migration yields a torn copy — and a torn copy looks exactly
    like a complete one, since nothing in the destination records that some of
    the source's nodes were written after it was read. The source's serial is
    re-checked before the write for the same reason: locks are taken after the
    read, so the read itself needs a way to prove it was not overtaken.

    The two locks are acquired in a deterministic order (by label), so two
    migrations running in opposite directions cannot deadlock on each other.
    """
    with closing(copy.source), closing(copy.destination):
        graph, outputs = copy.source.load(), copy.source.outputs()
        existing = len(copy.destination.load())
        if existing and not force:
            fail(
                f"{copy.destination_label} already holds {existing} node(s) — refusing "
                f"to overwrite it. Pass --force to replace it, or point at an empty "
                f"destination"
            )
        require_confirm(
            confirm,
            f"copy {len(graph)} node(s) from {copy.source_label} to "
            f"{copy.destination_label}"
            + (f", replacing {existing} node(s) there" if existing else "")
            + "?",
        )
        # The destination's scope must cover the nodes about to *arrive*, not only
        # those already there: an empty destination would lock nothing and then be
        # written outside its own lease.
        incoming = frozenset(graph.nodes)
        sides = sorted(
            [
                (copy.source_label, copy.source, incoming),
                (
                    copy.destination_label,
                    copy.destination,
                    incoming | frozenset(copy.destination.load().nodes),
                ),
            ],
            key=lambda side: side[0],
        )
        with ExitStack() as locks:
            for _, backend, scope in sides:
                locks.enter_context(held_lock(backend, scope, policy=policy))
            before = copy.source.serial()
            graph, outputs = copy.source.load(), copy.source.outputs()
            # Replace, not merge: `put_many` is an upsert, so destination-only
            # rows would survive a --force migration as phantom nodes the next
            # apply/destroy acts on. `replace_many` drops them in the same write.
            obsolete = [nid for nid in copy.destination.load().nodes if nid not in graph.nodes]
            copy.destination.replace_many(obsolete, graph.nodes.values())
            stale_outputs = [key for key in copy.destination.outputs() if key not in outputs]
            if outputs or stale_outputs:
                copy.destination.set_outputs(outputs, remove=stale_outputs)
            if copy.source.serial() != before:
                fail(
                    f"{copy.source_label} changed while it was being copied — the "
                    f"destination may be incomplete. Re-run the migration"
                )
    console.print(
        f"[green]migrated[/] {len(graph)} node(s) to {escape(copy.destination_label)}\n"
        f"[dim]{escape(copy.epilogue)}[/]"
    )


# -- unlock -------------------------------------------------------------------


@app.command("unlock")
def unlock(
    node: Annotated[
        list[str] | None,
        typer.Option("--node", help="Break the hold on this node id (repeatable)."),
    ] = None,
    owner: Annotated[
        str | None, typer.Option("--owner", help="Break every hold held by this owner.")
    ] = None,
    every: Annotated[
        bool, typer.Option("--all", help="Break every hold recorded in the backend.")
    ] = False,
    confirm: ConfirmOpt = False,
) -> None:
    """Show who holds the state lock, and break a hold left behind by a dead run.

    A lease outlives the run that took it: a killed CI job blocks its teammates
    until the TTL lapses, with nothing to do but wait. With no selector this only
    lists the holds — breaking one while its run is alive lets two applies write
    the same resources, so it names the holder and asks first.
    """
    target = _announced_target()
    with closing(target.open()) as backend:
        held = backend.locks()
        if not held:
            console.print("[dim]no locks held[/]")
            return
        _render_locks(held)
        if not (node or owner or every):
            console.print(
                "\n[dim]pass --node/--owner/--all to break a hold "
                "(only when you know the run is gone)[/]"
            )
            return
        targets = _selected(held, node, owner, every)
        require_confirm(confirm, f"\nBreak {len(targets)} lock(s)?")
        broken = backend.force_unlock(targets)
    console.print(f"[green]unlocked[/] {broken} node(s)")


def _render_locks(held: dict[str, Lease]) -> None:
    now = time.time()
    table = Table(title="State locks")
    table.add_column("node", style="bold")
    table.add_column("owner")
    table.add_column("expires in")
    for node_id in sorted(held):
        lease = held[node_id]
        remaining = lease.expires_at - now
        table.add_row(
            node_id, lease.owner, f"{remaining:.0f}s" if remaining > 0 else "[dim]expired[/]"
        )
    console.print(table)


def _selected(
    held: dict[str, Lease], nodes: list[str] | None, owner: str | None, every: bool
) -> set[str]:
    """The node ids the selectors name. A selector that matches nothing is an error,
    so a typo cannot read as "broke everything you asked for"."""
    if every:
        return set(held)
    selected: set[str] = set()
    if owner is not None:
        selected |= {nid for nid, lease in held.items() if lease.owner == owner}
        if not selected:
            fail(f"no locks held by {owner!r}")
    if nodes:
        unknown = sorted(set(nodes) - set(held))
        if unknown:
            fail(f"not locked: {', '.join(unknown)}")
        selected |= set(nodes)
    return selected
