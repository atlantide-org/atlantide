"""``atlantide init`` — scaffold a project that compiles on the first command.

Flag-driven, with no prompts. That matches the stance the rest of the CLI takes
in :mod:`atlantide.cli.options`, which refuses an ``ATLANTIDE_CONFIRM`` and an
``ATLANTIDE_VAR_*`` on the grounds that a value which changes what runs must be
visible in the command or in a file under review. What ``init`` writes is exactly
such a value: it becomes ``atlantide.toml``, and every later command obeys it
silently. A wizard would also make ``init`` behave one way on a laptop and
another in CI, which is the failure ``require_confirm``'s non-tty check exists to
prevent.

Nothing is written until every check has passed. A scaffolder that fails halfway
leaves a directory that is neither empty nor a project, and the user has to work
out which of the two it is before they can retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from atlantide.cli.console import console
from atlantide.cli.errors import fail, fail_error, require_choice, unwrap_or_diag
from atlantide.cli.json_out import emit_json
from atlantide.cli.options import JsonOpt, RegionOpt, resolve_inputs
from atlantide.cli.project import PROJECT_FILENAME, find_project_file, load_project
from atlantide.cli.templates import (
    CONFIG_FILENAME,
    GITIGNORE_MARKER,
    MINIMAL,
    TEMPLATE_NAMES,
    TEMPLATES,
    render_gitignore,
    render_toml,
)
from atlantide.cli.wiring import machine_readable, stateless_engine
from atlantide.core.errors import AtlantideError
from atlantide.lang.builtins import slugify
from atlantide.secrets import SecretsConfig
from atlantide.secrets.factory import PROVIDERS as SECRETS_PROVIDERS
from atlantide.state import StateConfig
from atlantide.state.factory import BACKENDS as STATE_BACKENDS
from atlantide.state.factory import LOCAL, S3

app = typer.Typer()

#: Used when the target directory's name slugifies to nothing usable.
_FALLBACK_PREFIX = "atlantide"

GITIGNORE_FILENAME = ".gitignore"


@dataclass(frozen=True, slots=True)
class _File:
    """One file the scaffold will write, and how.

    ``append`` exists because ``.gitignore`` is the one file that may already
    belong to someone else: overwriting it would throw away their rules, so the
    atlantide block is added to what is there. Modelling that as a mode rather
    than as a special case keeps the collision check, the write and the report
    from each having to remember which file is the odd one.
    """

    path: Path
    content: str
    append: bool = False

    @property
    def collides(self) -> bool:
        """Whether writing this would destroy something already on disk."""
        return self.path.exists() and not self.append


@dataclass(frozen=True, slots=True)
class _Plan:
    """Everything ``init`` intends to write, rendered before anything is written."""

    directory: Path
    files: tuple[_File, ...]


@app.command("init")
def init(
    directory: Annotated[
        Path, typer.Argument(help="Directory to scaffold (created if absent).")
    ] = Path("."),
    template: Annotated[
        str,
        typer.Option("--template", "-t", help=f"Starter project: {' | '.join(TEMPLATE_NAMES)}."),
    ] = MINIMAL,
    state: Annotated[
        str, typer.Option("--state", help="State backend: local | s3 | postgres.")
    ] = LOCAL,
    bucket: Annotated[str | None, typer.Option("--bucket", help="s3: state bucket.")] = None,
    key: Annotated[str | None, typer.Option("--key", help="s3: state object key.")] = None,
    lock_table: Annotated[
        str | None, typer.Option("--lock-table", help="s3: DynamoDB table holding the leases.")
    ] = None,
    dsn: Annotated[str | None, typer.Option("--dsn", help="postgres: connection string.")] = None,
    schema: Annotated[
        str | None, typer.Option("--schema", help="postgres: schema holding the tables.")
    ] = None,
    secrets: Annotated[
        str, typer.Option("--secrets", help="Secrets provider: keyfile | env | ssm.")
    ] = "keyfile",
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="ssm: prepended to each secret name.")
    ] = None,
    region: RegionOpt = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite existing files and allow nesting.")
    ] = False,
    validate: Annotated[
        bool,
        typer.Option("--validate/--no-validate", help="Compile the generated config."),
    ] = True,
    json_out: JsonOpt = False,
) -> None:
    """Scaffold a new project: `atlantide.toml`, a starter config, and a `.gitignore`.

    The `minimal` template uses the local provider, so the scaffolded project
    applies with no cloud credentials at all — `atlantide apply` then `atlantide
    plan` is the fastest way to see the engine skip an unchanged graph.
    """
    machine_readable(json_out)
    require_choice(template, TEMPLATE_NAMES, "--template")
    require_choice(state, STATE_BACKENDS, "--state")
    require_choice(secrets, SECRETS_PROVIDERS, "--secrets")

    state_config = StateConfig(
        backend=state,
        bucket=bucket,
        key=key,
        lock_table=lock_table,
        dsn=dsn,
        schema=schema,
        region=region if state == S3 else None,
    )
    secrets_config = SecretsConfig(provider=secrets, prefix=prefix or "", region=region)
    # Validated by the backends themselves, so `--state s3` with no `--bucket`
    # fails with the message state/factory.py already owns. `init` deliberately
    # keeps no second copy of which keys a backend requires.
    try:
        state_config.validate()
        secrets_config.validate()
    except AtlantideError as exc:
        fail_error(exc)

    target = directory.resolve()
    _check_not_nested(target, force=force)
    plan = _render(target, template, state_config, secrets_config, region)
    _check_collisions(plan, force=force)
    _write(plan)

    if not json_out:
        _report_created(plan)
    if validate:
        _compile_check(target, json_out=json_out)
    if json_out:
        written = sorted(f.path.name for f in plan.files)
        emit_json({"directory": str(target), "template": template, "created": written})
        return
    _report_next(target)


# -- gates --------------------------------------------------------------------


def _check_not_nested(target: Path, *, force: bool) -> None:
    """Refuse to scaffold *inside* an existing project.

    ``atlantide.toml`` is found by walking up, so a second one below an existing
    project silently shadows it for every command run from there down. That is a
    hard mistake to see and an easy one to make when a subdirectory looks like a
    fresh start.

    A toml in ``target`` itself is not this problem — it is an ordinary file
    collision, and :func:`_check_collisions` reports it together with whatever
    else is in the way rather than stopping at the first thing it finds.
    """
    if force:
        return
    found = find_project_file(target)
    if found is None or found.parent == target:
        return
    fail(
        f"{target} sits inside the atlantide project at {found.parent}; a nested "
        f"{PROJECT_FILENAME} shadows it for every command run below here. Use --force if "
        f"that is intended."
    )


def _check_collisions(plan: _Plan, *, force: bool) -> None:
    """Refuse when any file already exists, naming all of them at once.

    All of them, because a user who has to re-run three times to discover three
    collisions has been told the truth three times and helped none.
    """
    if force:
        return
    if existing := sorted(f.path.name for f in plan.files if f.collides):
        fail(f"already exists in {plan.directory}: {', '.join(existing)} — use --force")


# -- rendering and writing ----------------------------------------------------


def _render(
    target: Path,
    template: str,
    state: StateConfig,
    secrets: SecretsConfig,
    region: str | None,
) -> _Plan:
    """Everything the scaffold consists of, held in memory before any write."""
    starter = TEMPLATES[template]
    inputs = {key: _name_prefix(target) for key in starter.inputs}
    files = [
        _File(
            target / PROJECT_FILENAME,
            render_toml(state=state, secrets=secrets, inputs=inputs, aws_region=region),
        ),
        _File(target / CONFIG_FILENAME, starter.config),
    ]
    if (gitignore := _gitignore(target)) is not None:
        files.append(gitignore)
    return _Plan(target, tuple(files))


def _gitignore(target: Path) -> _File | None:
    """The ``.gitignore`` entry, or ``None`` when the block is already there.

    Appending a second copy would work and look fine, which is exactly why it
    needs guarding: ``init --force`` run twice should leave one block.
    """
    path = target / GITIGNORE_FILENAME
    if not path.exists():
        return _File(path, render_gitignore())
    existing = path.read_text()
    if GITIGNORE_MARKER in existing:
        return None
    separator = "" if existing.endswith("\n") else "\n"
    return _File(path, f"{separator}\n{render_gitignore()}", append=True)


def _name_prefix(target: Path) -> str:
    """A resource name prefix derived from the project directory.

    Slugified because it lands in S3 bucket names, which are far stricter than
    directory names are.
    """
    return slugify(target.name) or _FALLBACK_PREFIX


def _write(plan: _Plan) -> None:
    """Create the directory and write every file."""
    plan.directory.mkdir(parents=True, exist_ok=True)
    for entry in plan.files:
        with entry.path.open("a" if entry.append else "w") as handle:
            handle.write(entry.content)


# -- post-write verification --------------------------------------------------


def _compile_check(target: Path, *, json_out: bool) -> None:
    """Compile the config that was just written, through the engine `validate` uses.

    In-process rather than a subprocess: it keeps the typed ``Result``, and
    ``sys.argv[0]`` is not ``atlantide`` inside the PyInstaller binary. The engine
    is stateless — a memory backend, no lock, no credentials — so this is safe
    even when the project was scaffolded against s3 or postgres.

    A failure here means a shipped template is broken, so the files stay on disk:
    that is a bug report, and deleting the evidence would not help anyone file it.
    """
    project = load_project(target)
    config_path = target / CONFIG_FILENAME
    source = config_path.read_text()
    with stateless_engine(project) as engine:
        compiled = unwrap_or_diag(
            engine.compile(
                source, str(config_path), inputs=resolve_inputs(project.inputs, None, None)
            ),
            source,
        )
    if not json_out:
        console.print(
            f"[green]ok[/] {escape(CONFIG_FILENAME)} — "
            f"{len(compiled.ir.nodes)} resource(s), no cycles"
        )


def _report_created(plan: _Plan) -> None:
    """What was written, before the compile check reports on it."""
    for entry in sorted(plan.files, key=lambda f: f.path.name):
        verb = "appended" if entry.append else "created"
        console.print(f"[green]{verb}[/]  {escape(str(entry.path))}")


def _report_next(target: Path) -> None:
    """The one command that comes next."""
    location = "" if target == Path.cwd() else f"cd {target} && "
    console.print(f"\n[dim]next:[/]  {escape(location)}atlantide plan")
