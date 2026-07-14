"""``atlantide component`` — manage published components (git-pinned L2 constructs).

Four verbs, mirroring a package manager over the ``[components.*]`` sources in
``atlantide.toml`` and the resolved pins in ``atlantide.lock``:

* ``add``    — declare a git source, fetch it, and pin it (one-shot onboarding).
* ``lock``   — resolve every declared ref to an exact commit + hash.
* ``vendor`` — rematerialize ``.atlantis/components`` from the lock.
* ``verify`` — re-hash the vendored trees and check them against the lock.

Config then imports a component as ``atlantide.components.<alias>``; the mount that
makes that resolve lives in :mod:`atlantide.components`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar

import typer

from atlantide.cli.console import console
from atlantide.cli.errors import fail, fail_error
from atlantide.cli.project import load_project
from atlantide.components import fetch as fetch_mod
from atlantide.components.lock import load_lock, write_lock
from atlantide.components.source import ComponentSource
from atlantide.core import ComponentError

app = typer.Typer(help="Manage published components (git-pinned, imported by config).")

_T = TypeVar("_T")
_ALIAS_RE = re.compile(r"[^0-9a-zA-Z_]+")


def _guard(action: Callable[..., _T], *args: object) -> _T:
    """Run a fetch-layer call, turning any ``ComponentError`` into a CLI exit."""
    try:
        return action(*args)
    except ComponentError as exc:
        fail_error(exc)  # NoReturn


def _default_alias(git: str) -> str:
    """Derive a module-safe import alias from a git URL's last path segment."""
    tail = git.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    alias = _ALIAS_RE.sub("_", tail).strip("_").lower()
    if not alias or not alias[0].isalpha():
        fail(f"cannot derive an alias from {git!r}; pass --as <name>")
    return alias


def _append_source(root: Path, alias: str, source: ComponentSource) -> None:
    """Append a ``[components.<alias>]`` block to ``atlantide.toml`` (create if absent)."""
    lines = [f"\n[components.{alias}]", f'git = "{source.git}"']
    if source.ref:
        lines.append(f'ref = "{source.ref}"')
    if source.subdir:
        lines.append(f'subdir = "{source.subdir}"')
    path = root / "atlantide.toml"
    existing = path.read_text() if path.exists() else ""
    path.write_text(existing + "\n".join(lines) + "\n")


@app.command("add")
def add(
    git: Annotated[str, typer.Argument(help="Public git URL of the component repo.")],
    ref: Annotated[
        str | None, typer.Option("--ref", help="Tag/branch/commit to pin (default: repo HEAD).")
    ] = None,
    alias: Annotated[
        str | None, typer.Option("--as", help="Import alias (default: derived from the URL).")
    ] = None,
    subdir: Annotated[
        str | None, typer.Option("--subdir", help="Package location within the repo.")
    ] = None,
) -> None:
    """Declare a component source, fetch it, and pin it in atlantide.lock.

    Imported from config as ``from atlantide.components.<alias> import ...``.
    """
    alias = alias or _default_alias(git)
    root = Path.cwd()
    if alias in load_project(root).components:
        fail(f"component {alias!r} is already declared in atlantide.toml")

    source = ComponentSource(git=git, ref=ref, subdir=subdir)
    # Fetch first so a bad repo/ref fails before touching atlantide.toml.
    entry = _guard(fetch_mod.fetch, alias, source, root)
    _append_source(root, alias, source)
    write_lock(root, {**load_lock(root), alias: entry})
    console.print(
        f"[green]added[/] {alias!r} @ {entry.commit[:12]} "
        f"— import as [bold]atlantide.components.{alias}[/]"
    )


@app.command("lock")
def lock() -> None:
    """Resolve every declared source's ref to an exact commit + content hash."""
    root = Path.cwd()
    sources = load_project(root).components
    if not sources:
        console.print("[dim]no components declared in atlantide.toml[/]")
        return
    entries = {}
    for alias, source in sources.items():
        entries[alias] = entry = _guard(fetch_mod.fetch, alias, source, root)
        console.print(f"[green]locked[/] {alias} @ {entry.commit[:12]}")
    write_lock(root, entries)


@app.command("vendor")
def vendor() -> None:
    """Rematerialize .atlantis/components from atlantide.lock."""
    root = Path.cwd()
    entries = load_lock(root)
    if not entries:
        fail("no atlantide.lock found; run `atlantide component lock` first")
    for alias, entry in entries.items():
        _guard(fetch_mod.vendor, alias, entry, root)
        console.print(f"[green]vendored[/] {alias}")


@app.command("verify")
def verify() -> None:
    """Re-hash the vendored trees and check them against the lock (tamper/drift)."""
    root = Path.cwd()
    entries = load_lock(root)
    if not entries:
        fail("no atlantide.lock found; nothing to verify")
    for alias, entry in entries.items():
        _guard(fetch_mod.verify, alias, entry, root)
        console.print(f"[green]ok[/] {alias}: hash matches lock")
