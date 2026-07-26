"""The `.atlas` artifact lifecycle: build, verify, deploy.

One topic, and the only consumers of the artifact codec. `main` registers
these on the Typer app so command names and help are unchanged."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, cast

import typer

from atlantide.cli.console import console
from atlantide.cli.errors import (
    fail,
    require_choice,
    run_async,
    unwrap_or_diag,
    unwrap_or_exit,
)
from atlantide.cli.options import (
    ON_FAILURE_CHOICES,
    ConfigArg,
    ConfirmOpt,
    ParallelismOpt,
    RegionOpt,
    StateOpt,
    VarFileOpt,
    VarOpt,
    require_confirm,
)
from atlantide.cli.progress import maybe_live
from atlantide.cli.render import (
    render_report,
)
from atlantide.cli.target import load_project
from atlantide.cli.wiring import (
    config_run,
    engine_for,
    stateless_engine,
)
from atlantide.cli.wiring import target as state_target
from atlantide.components.lock import load_lock
from atlantide.ir import Artifact
from atlantide.ir import loads as _load_artifact_text
from atlantide.reconcile import OnFailure


def build(
    config: ConfigArg = None,
    var: VarOpt = None,
    var_file: VarFileOpt = None,
    output: Annotated[Path, typer.Option("--output", "-o", help="Artifact path to write.")] = Path(
        "out.atlas"
    ),
) -> None:
    """Compile a config into a portable, content-hashed .atlas artifact."""
    run = config_run(config, var, var_file)
    project = run.project
    # The project root, not cwd: `load_project` walks up to atlantide.toml, so a
    # `build` run from a subdirectory would find no lock and record no pins.
    component_pins = {alias: entry.commit for alias, entry in load_lock(project.directory).items()}
    with stateless_engine(project) as engine:  # build needs no state
        artifact = unwrap_or_diag(
            engine.build(
                run.source, str(run.path), inputs=run.inputs, component_pins=component_pins
            ),
            run.source,
        )
        output.write_text(artifact.dumps())
    console.print(
        f"[green]built[/] {output} — {len(artifact.ir)} nodes, "
        f"hash {artifact.ir_hash[:12]}…, pins {artifact.provider_pins}"
    )


def verify(
    artifact: Annotated[Path, typer.Argument(help="Path to a .atlas artifact.")],
) -> None:
    """Check an artifact's IR hash and that its pinned providers are compatible."""
    art = _read_artifact(artifact)
    with stateless_engine(load_project()) as engine:
        unwrap_or_exit(engine.verify_artifact(art))
    console.print(f"[green]ok[/] {artifact}: hash and provider pins verified")


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
    require_choice(on_failure, ON_FAILURE_CHOICES, "--on-failure")
    art = _read_artifact(artifact)
    require_confirm(confirm, f"Deploy {artifact} ({len(art.ir)} nodes)?")
    project = load_project()
    with engine_for(state_target(state, project), region=region, parallelism=parallelism) as engine:
        started = time.perf_counter()
        # `[]` rather than a seeded list: deploy learns its nodes as they start.
        with maybe_live([], enabled=console.is_terminal) as progress:
            result = run_async(
                engine.deploy(art, on_failure=cast(OnFailure, on_failure), progress=progress)
            )
        render_report(
            unwrap_or_exit(result),
            elapsed=time.perf_counter() - started,
            show_nodes=not console.is_terminal,
        )


def _read_artifact(path: Path) -> Artifact:
    if not path.exists():
        fail(f"artifact not found: {path}")
    return unwrap_or_exit(_load_artifact_text(path.read_text()))
