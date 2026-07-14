"""atlantide command-line interface.

Commands: ``plan`` | ``apply`` | ``destroy`` | ``refresh`` | ``graph``; ``build`` |
``verify`` | ``deploy`` (portable ``.atlas`` artifacts); ``resources`` | ``schema``;
``component`` (published components) | ``secret`` (local secrets store).
Config file and state db can be set in ``atlantide.toml``
(:mod:`atlantide.cli.project`).

Rendering lives in :mod:`atlantide.cli.render` / ``json_out`` / ``diagram`` /
``progress``; error plumbing in :mod:`atlantide.cli.errors`. This module holds
the command definitions and the engine/provider wiring.
"""

from __future__ import annotations

import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Annotated, Any, cast, get_args

import typer
from rich.table import Table

from atlantide.cli.component import app as component_app
from atlantide.cli.console import console
from atlantide.cli.diagram import to_dot, to_mermaid
from atlantide.cli.errors import (
    fail,
    require_choice,
    run_async,
    set_debug,
    unwrap_or_diag,
    unwrap_or_exit,
)
from atlantide.cli.introspect import all_types, schema_rows
from atlantide.cli.json_out import drift_json, emit_json, plan_json, report_json
from atlantide.cli.progress import live_apply
from atlantide.cli.project import ProjectConfig, load_project
from atlantide.cli.render import (
    MUT_COLOR,
    render_destroy_preview,
    render_drift,
    render_plan,
    render_report,
)
from atlantide.components import mount as mount_components
from atlantide.components.lock import load_lock
from atlantide.core import AtlantideError, ProviderRegistry
from atlantide.engine import Engine
from atlantide.ir import Artifact
from atlantide.ir import loads as _load_artifact_text
from atlantide.providers.aws import AwsAlias, AwsProvider
from atlantide.providers.local import LocalProvider
from atlantide.providers.random import RandomProvider
from atlantide.reconcile import Action, OnFailure
from atlantide.secrets import KeyfileValueStore, KeyMaterial, SecretsRegistry
from atlantide.state import MemoryStateBackend, SqliteStateBackend

app = typer.Typer(add_completion=True, help="Atlantide — typed, deterministic IaC.")


def _version() -> str:
    try:
        return _pkg_version("atlantide")
    except PackageNotFoundError:  # not installed (e.g. running from source tree)
        from atlantide import __version__

        return __version__


def _version_callback(show: bool) -> None:
    if show:
        console.print(f"atlantide {_version()}")
        raise typer.Exit(0)


@app.callback()
def _main(
    _version_flag: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True,
                     help="Show the version and exit."),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option("--debug", help="On error, print the full traceback and cause chain."),
    ] = False,
) -> None:
    """Atlantide — typed, deterministic IaC."""
    set_debug(debug)
    # Make any vendored published components importable as `atlantide.components.*`
    # before a config is evaluated. No-op until `atlantide component vendor` has run.
    mount_components(Path.cwd())


ConfigArg = Annotated[Path | None, typer.Argument(help="Atlas-lang config (.py).")]
StateOpt = Annotated[Path | None, typer.Option("--state", help="State database file.")]
ConfirmOpt = Annotated[
    bool,
    typer.Option("--confirm", "-y", help="Skip the interactive confirmation prompt."),
]

_DEFAULT_STATE = Path("atlantide.db")
_ON_FAILURE: tuple[str, ...] = get_args(OnFailure)

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


def _require_confirm(confirm: bool, question: str) -> None:
    """Prompt before a mutating action unless ``--confirm`` was passed (aborts on no)."""
    if not confirm:
        typer.confirm(question, abort=True)


# Engine / provider / secrets wiring; one load_project() per command.


def _providers(
    project: ProjectConfig, region: str | None = None
) -> tuple[ProviderRegistry, dict[str, Any]]:
    """Build the provider registry; AWS region/profile/endpoint from flag or toml."""
    aws_kwargs: dict[str, Any] = {}
    if resolved_region := (region or project.aws_region):
        aws_kwargs["region"] = resolved_region
    if project.aws_profile:
        aws_kwargs["profile"] = project.aws_profile
    if project.aws_endpoint:
        aws_kwargs["endpoint_url"] = project.aws_endpoint
    if project.aws_aliases:
        aws_kwargs["aliases"] = {
            name: AwsAlias(profile=cfg.get("profile"), endpoint_url=cfg.get("endpoint"))
            for name, cfg in project.aws_aliases.items()
        }
    registry = ProviderRegistry()
    registry.register(LocalProvider())
    registry.register(RandomProvider())
    registry.register(AwsProvider(**aws_kwargs))
    return registry, all_types()


def _store_and_key(state_path: Path, project: ProjectConfig) -> tuple[Path, Path]:
    """The keyfile value-store and encryption-key paths (flag/toml, else beside the db)."""
    base = state_path.parent
    store = Path(project.secrets_store) if project.secrets_store else base / "atlantide.secrets"
    key = Path(project.secrets_key) if project.secrets_key else base / "atlantide.key"
    return store, key


def _value_store(state_path: Path, project: ProjectConfig) -> KeyfileValueStore:
    """The local keyfile value-store."""
    store, key = _store_and_key(state_path, project)
    return KeyfileValueStore(store, key)


def _secrets(state_path: Path, project: ProjectConfig) -> SecretsRegistry:
    """Default secrets registry: the local keyfile value-store, plus install key
    material (per-install digest salt + at-rest sealing of sensitive outputs).

    The keyfile is loaded lazily by the material, so a project with no secrets
    and no sensitive outputs never creates a key.
    """
    store, key = _store_and_key(state_path, project)
    registry = SecretsRegistry(material=KeyMaterial(str(key)))
    registry.register(KeyfileValueStore(store, key), default=True)
    return registry


def _engine(
    state_path: Path,
    project: ProjectConfig,
    *,
    region: str | None = None,
    parallelism: int | None = None,
) -> Engine:
    providers, types = _providers(project, region)
    par = parallelism or project.parallelism
    return Engine(
        providers, SqliteStateBackend(str(state_path)), types,
        secrets=_secrets(state_path, project), parallelism=par,
    )


def _stateless_engine(project: ProjectConfig) -> Engine:
    """Engine for compile-only commands (graph/build); touches no state or keyfile."""
    providers, types = _providers(project)
    return Engine(providers, MemoryStateBackend(), types)


# Config resolution: explicit flag -> atlantide.toml -> default.


def _resolve_config(config: Path | None, project: ProjectConfig) -> Path:
    if config is not None:
        return config
    if project.config:
        return Path(project.config)
    fail("no config given and none set in atlantide.toml (expected a .py path)")


def _resolve_state(state: Path | None, project: ProjectConfig) -> Path:
    if state is not None:
        return state
    return Path(project.state) if project.state else _DEFAULT_STATE


@app.command()
def plan(
    config: ConfigArg = None,
    state: StateOpt = None,
    json_out: JsonOpt = False,
    detailed_exitcode: Annotated[
        bool,
        typer.Option(
            "--detailed-exitcode",
            help="Exit 0 (no changes), 2 (changes pending), 1 (error/denied).",
        ),
    ] = False,
) -> None:
    """Show the changes a config would make against current state.

    Exits non-zero when a mandatory policy denies the plan. With
    --detailed-exitcode, also exits 2 when changes are pending.
    """
    project = load_project()
    cfg = _resolve_config(config, project)
    with _engine(_resolve_state(state, project), project) as engine:
        source = cfg.read_text()
        plan_obj = unwrap_or_diag(engine.plan(source, str(cfg)), source)
        if json_out:
            emit_json(plan_json(plan_obj))
        else:
            render_plan(plan_obj)
        if plan_obj.blocked:
            raise typer.Exit(1)  # a mandatory policy denies this plan
        if detailed_exitcode and plan_obj.changeset.actionable:
            raise typer.Exit(2)  # changes pending


@app.command()
def graph(
    config: ConfigArg = None,
    fmt: Annotated[str, typer.Option("--format", help="mermaid | dot")] = "mermaid",
) -> None:
    """Print the resource dependency graph (Graphviz dot or Mermaid)."""
    require_choice(fmt, ("dot", "mermaid"), "format")
    project = load_project()
    cfg = _resolve_config(config, project)
    with _stateless_engine(project) as engine:
        source = cfg.read_text()
        compiled = unwrap_or_diag(engine.compile(source, str(cfg)), source)
        digraph = compiled.graph
        rendered = to_dot(digraph) if fmt == "dot" else to_mermaid(digraph)
        console.print(rendered, markup=False, highlight=False)


@app.command()
def apply(
    config: ConfigArg = None,
    state: StateOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan without making changes.")
    ] = False,
    confirm: ConfirmOpt = False,
    json_out: JsonOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
    on_failure: Annotated[
        str,
        typer.Option(
            "--on-failure",
            help="On a provider error: 'rollback' (undo completed nodes, saga; "
            "default) or 'halt' (leave completed nodes in place).",
        ),
    ] = "rollback",
) -> None:
    """Apply a config: create/update/replace/delete resources to match it.

    Shows the plan and asks for confirmation before applying; pass --confirm/-y
    (or --dry-run) to skip the prompt.
    """
    require_choice(on_failure, _ON_FAILURE, "--on-failure")
    project = load_project()
    cfg = _resolve_config(config, project)
    with _engine(
        _resolve_state(state, project), project, region=region, parallelism=parallelism
    ) as engine:
        source = cfg.read_text()
        plan_obj = unwrap_or_diag(engine.plan(source, str(cfg)), source)
        if not json_out:
            render_plan(plan_obj)
        if dry_run:
            if not json_out:
                console.print("[dim](dry run — no changes made)[/]")
            return
        if not plan_obj.changeset.actionable:
            if not json_out:
                console.print("[dim]nothing to apply[/]")
            return
        _require_confirm(confirm, "\nApply these changes?")
        actionable = [(c.node_id, c.action) for c in plan_obj.changeset.actionable]
        used_live = console.is_terminal and not json_out
        started = time.perf_counter()
        if used_live:
            with live_apply(actionable) as progress:
                result = run_async(
                    engine.apply(
                        source, str(cfg), on_failure=cast(OnFailure, on_failure), progress=progress
                    )
                )
        else:
            result = run_async(
                engine.apply(source, str(cfg), on_failure=cast(OnFailure, on_failure))
            )
        report = unwrap_or_diag(result, source)
        if json_out:
            emit_json(report_json(report))
        else:
            render_report(report, elapsed=time.perf_counter() - started, show_nodes=not used_live)


@app.command()
def build(
    config: ConfigArg = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Artifact path to write.")
    ] = Path("out.atlas"),
) -> None:
    """Compile a config into a portable, content-hashed .atlas artifact."""
    project = load_project()
    cfg = _resolve_config(config, project)
    component_pins = {
        alias: entry.commit for alias, entry in load_lock(Path.cwd()).items()
    }
    with _stateless_engine(project) as engine:  # build needs no state
        source = cfg.read_text()
        artifact = unwrap_or_diag(
            engine.build(source, str(cfg), component_pins=component_pins), source
        )
        output.write_text(artifact.dumps())
    console.print(
        f"[green]built[/] {output} — {len(artifact.ir)} nodes, "
        f"hash {artifact.ir_hash[:12]}…, pins {artifact.provider_pins}"
    )


@app.command()
def verify(
    artifact: Annotated[Path, typer.Argument(help="Path to a .atlas artifact.")],
) -> None:
    """Check an artifact's IR hash and that its pinned providers are compatible."""
    art = _read_artifact(artifact)
    with _stateless_engine(load_project()) as engine:
        unwrap_or_exit(engine.verify_artifact(art))
    console.print(f"[green]ok[/] {artifact}: hash and provider pins verified")


@app.command()
def deploy(
    artifact: Annotated[Path, typer.Argument(help="Path to a .atlas artifact.")],
    state: StateOpt = None,
    confirm: ConfirmOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
    on_failure: Annotated[
        str, typer.Option("--on-failure", help="'rollback' (default) or 'halt' on provider error.")
    ] = "rollback",
) -> None:
    """Apply a .atlas artifact directly — no source, no config re-execution."""
    require_choice(on_failure, _ON_FAILURE, "--on-failure")
    art = _read_artifact(artifact)
    _require_confirm(confirm, f"Deploy {artifact} ({len(art.ir)} nodes)?")
    project = load_project()
    with _engine(
        _resolve_state(state, project), project, region=region, parallelism=parallelism
    ) as engine:
        started = time.perf_counter()
        if console.is_terminal:
            with live_apply([]) as progress:  # rows appear as nodes start
                result = run_async(engine.deploy(art, on_failure=cast(OnFailure, on_failure),
                                                 progress=progress))
        else:
            result = run_async(engine.deploy(art, on_failure=cast(OnFailure, on_failure)))
        render_report(
            unwrap_or_exit(result),
            elapsed=time.perf_counter() - started,
            show_nodes=not console.is_terminal,
        )


def _read_artifact(path: Path) -> Artifact:
    if not path.exists():
        fail(f"artifact not found: {path}")
    return unwrap_or_exit(_load_artifact_text(path.read_text()))


@app.command()
def destroy(
    state: StateOpt = None,
    confirm: ConfirmOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
) -> None:
    """Destroy every resource recorded in state (shows what, then prompts)."""
    project = load_project()
    with _engine(
        _resolve_state(state, project), project, region=region, parallelism=parallelism
    ) as engine:
        node_ids = sorted(engine.backend.load().nodes)
        if not node_ids:
            console.print("[dim]nothing in state to destroy[/]")
            return
        render_destroy_preview(node_ids)  # show what will be removed first
        _require_confirm(confirm, f"\nDestroy these {len(node_ids)} resource(s)?")
        started = time.perf_counter()
        if console.is_terminal:
            with live_apply([(nid, Action.DELETE) for nid in node_ids]) as progress:
                result = run_async(engine.destroy(progress=progress))
        else:
            result = run_async(engine.destroy())
        report = unwrap_or_exit(result)
        render_report(
            report,
            elapsed=time.perf_counter() - started,
            title="Destroyed",
            summary=f"{len(report.deleted)} resource(s)",
            show_nodes=not console.is_terminal,
        )


@app.command()
def refresh(
    state: StateOpt = None,
    write: Annotated[
        bool,
        typer.Option("--write", help="Sync detected drift back into state (default: report only)."),
    ] = False,
    json_out: JsonOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
    detailed_exitcode: Annotated[
        bool,
        typer.Option(
            "--detailed-exitcode",
            help="Exit 0 (no drift), 2 (drift found), 1 (error).",
        ),
    ] = False,
) -> None:
    """Read live provider state and report drift vs. recorded state.

    Read-only unless --write is given, in which case detected drift is synced
    back into state (drifted outputs overwritten, missing resources removed).
    """
    project = load_project()
    with _engine(
        _resolve_state(state, project), project, region=region, parallelism=parallelism
    ) as engine:
        if not engine.backend.load().nodes:
            if not json_out:
                console.print("[dim]nothing in state to refresh[/]")
            return
        report = unwrap_or_exit(run_async(engine.refresh(write=write)))
        if json_out:
            emit_json(drift_json(report))
        else:
            render_drift(report, wrote=write)
        if detailed_exitcode and report.has_drift:
            raise typer.Exit(2)


app.add_typer(component_app, name="component")

secret_app = typer.Typer(help="Manage the local secrets value-store (name → value).")
app.add_typer(secret_app, name="secret")


def _store_for(state: Path | None) -> KeyfileValueStore:
    project = load_project()
    return _value_store(_resolve_state(state, project), project)


@secret_app.command("set")
def secret_set(
    name: Annotated[str, typer.Argument(help="Secret name, e.g. app/signing-key.")],
    value: Annotated[str | None, typer.Argument(help="Value (prompted if omitted).")] = None,
    state: StateOpt = None,
) -> None:
    """Store a secret value locally (encrypted). Referenced by name via SecretRef."""
    plaintext = value if value is not None else typer.prompt("value", hide_input=True)
    _store_for(state).set(name, plaintext)
    console.print(f"[green]set[/] secret {name!r}")


@secret_app.command("rm")
def secret_rm(
    name: Annotated[str, typer.Argument(help="Secret name to remove.")],
    state: StateOpt = None,
) -> None:
    """Remove a secret from the local store."""
    if _store_for(state).delete(name):
        console.print(f"[green]removed[/] secret {name!r}")
    else:
        fail(f"no such secret {name!r}")


@secret_app.command("get")
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


@secret_app.command("list")
def secret_list(state: StateOpt = None) -> None:
    """List stored secret names (never their values)."""
    names = _store_for(state).names()
    if not names:
        console.print("[dim]no secrets stored[/]")
        return
    for name in names:
        console.print(name)


@app.command()
def resources() -> None:
    """List every resource type across the built-in providers."""
    types = all_types()
    table = Table(title="Resource types")
    table.add_column("type", style="bold")
    table.add_column("provider")
    table.add_column("fields", justify="right")
    for type_name in sorted(types):
        cls = types[type_name]
        table.add_row(type_name, cls.provider_name() or "-", str(len(schema_rows(cls))))
    console.print(table)


@app.command()
def schema(
    type_name: Annotated[str, typer.Argument(help="Resource type, e.g. aws.S3Bucket.")],
) -> None:
    """Show the fields of one resource type (type, mutability, default, sensitivity)."""
    types = all_types()
    cls = types.get(type_name)
    if cls is None:
        available = ", ".join(sorted(types))
        fail(f"unknown type {type_name!r}. Available: {available}")
    table = Table(title=type_name)
    table.add_column("field", style="bold")
    table.add_column("type")
    table.add_column("mutability")
    table.add_column("required")
    table.add_column("default")
    table.add_column("sensitive")
    for row in schema_rows(cls):
        color = MUT_COLOR[row.mutability]
        table.add_row(
            row.name,
            row.type,
            f"[{color}]{row.mutability.value}[/]",
            "yes" if row.required else "",
            row.default,
            "yes" if row.sensitive else "",
        )
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
