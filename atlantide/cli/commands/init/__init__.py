"""``atlantide init``: scaffold a project that compiles on the first command.

Flag-driven with no prompts, for the reason :mod:`atlantide.cli.options` gives for
having no ``ATLANTIDE_*`` variables: ``init`` writes ``atlantide.toml``, which every
later command reads. Flags also keep ``init`` identical in a terminal and in CI.

Nothing is written until every check passes, so a failure leaves no partial project.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from atlantide.cli.commands.init.templates import (
    CONFIG_FILENAME,
    GITIGNORE_MARKER,
    MINIMAL,
    TEMPLATE_NAMES,
    TEMPLATES,
    render_gitignore,
    render_toml,
)
from atlantide.cli.console import console
from atlantide.cli.context import set_json_mode
from atlantide.cli.errors import fail, fail_error, require_choice, unwrap_or_diag
from atlantide.cli.options import JsonOpt, RegionOpt, resolve_inputs
from atlantide.cli.project import load_project
from atlantide.cli.views.output import emit_json
from atlantide.cli.wiring import stateless_engine
from atlantide.core.errors import AtlantideError
from atlantide.lang.builtins import slugify
from atlantide.secrets import SecretsConfig
from atlantide.secrets.factory import PROVIDERS as SECRETS_PROVIDERS
from atlantide.state import StateConfig
from atlantide.state.factory import BACKENDS as STATE_BACKENDS
from atlantide.state.factory import DSN_ENV, LOCAL, S3
from atlantide.state.sql.dsn import dsn_password
from atlantide.util.project import PROJECT_FILENAME, find_project_file

app = typer.Typer()

#: Used when the target directory's name slugifies to nothing usable.
_FALLBACK_PREFIX = "atlantide"

GITIGNORE_FILENAME = ".gitignore"

DirectoryArg = Annotated[Path, typer.Argument(help="Directory to scaffold (created if absent).")]
TemplateOpt = Annotated[
    str,
    typer.Option("--template", "-t", help=f"Starter project: {' | '.join(TEMPLATE_NAMES)}."),
]
StateBackendOpt = Annotated[
    str, typer.Option("--state", help="State backend: local | s3 | postgres.")
]
BucketOpt = Annotated[str | None, typer.Option("--bucket", help="s3: state bucket.")]
KeyOpt = Annotated[str | None, typer.Option("--key", help="s3: state object key.")]
LockTableOpt = Annotated[
    str | None, typer.Option("--lock-table", help="s3: DynamoDB table holding the leases.")
]
DsnOpt = Annotated[str | None, typer.Option("--dsn", help="postgres: connection string.")]
SchemaOpt = Annotated[
    str | None, typer.Option("--schema", help="postgres: schema holding the tables.")
]
SecretsOpt = Annotated[
    str, typer.Option("--secrets", help="Secrets provider: keyfile | env | ssm.")
]
PrefixOpt = Annotated[
    str | None, typer.Option("--prefix", help="ssm: prepended to each secret name.")
]
ForceOpt = Annotated[
    bool, typer.Option("--force", help="Overwrite existing files and allow nesting.")
]
ValidateOpt = Annotated[
    bool,
    typer.Option("--validate/--no-validate", help="Compile the generated config."),
]


@dataclass(frozen=True, slots=True)
class _File:
    """One file the scaffold writes, and how.

    ``append`` adds the atlantide block to an existing ``.gitignore`` instead of
    overwriting the rules already in it.
    """

    path: Path
    content: str
    append: bool = False

    @property
    def collides(self) -> bool:
        """Whether writing this would overwrite an existing file."""
        return self.path.exists() and not self.append


@dataclass(frozen=True, slots=True)
class _Plan:
    """Everything ``init`` intends to write, rendered before anything is written."""

    directory: Path
    files: tuple[_File, ...]


@app.command("init")
def init(  # noqa: PLR0913 - Typer command: one parameter per CLI option
    directory: DirectoryArg = Path("."),
    template: TemplateOpt = MINIMAL,
    state: StateBackendOpt = LOCAL,
    bucket: BucketOpt = None,
    key: KeyOpt = None,
    lock_table: LockTableOpt = None,
    dsn: DsnOpt = None,
    schema: SchemaOpt = None,
    secrets: SecretsOpt = "keyfile",
    prefix: PrefixOpt = None,
    region: RegionOpt = None,
    force: ForceOpt = False,
    validate: ValidateOpt = True,
    json_out: JsonOpt = False,
) -> None:
    """Scaffold a new project: `atlantide.toml`, a starter config, and a `.gitignore`.

    The `minimal` template uses the local provider, so the scaffolded project
    applies with no cloud credentials. Run `atlantide apply` then `atlantide
    plan` to see the engine skip an unchanged graph.
    """
    set_json_mode(enabled=json_out)
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
    _check_configs(state_config, secrets_config)
    _check_dsn_has_no_password(dsn)

    target = directory.resolve()
    _check_not_nested(target, force=force)
    plan = _render(target, template, state_config, secrets_config, region)
    _check_collisions(plan, force=force)
    _write(plan)
    _report(plan, template, validate=validate, json_out=json_out)


# -- gates --------------------------------------------------------------------


def _check_configs(state: StateConfig, secrets: SecretsConfig) -> None:
    """Refuse a backend or secrets provider missing a key it requires.

    Delegates to each config's ``validate()``, so the required keys are defined only
    in :mod:`atlantide.state.factory` and :mod:`atlantide.secrets.factory`.
    """
    try:
        state.validate()
        secrets.validate()
    except AtlantideError as exc:
        fail_error(exc)


def _check_dsn_has_no_password(dsn: str | None) -> None:
    """Refuse to write a password into ``atlantide.toml``.

    The file is meant to be committed; the full dsn can come from the environment
    instead.
    """
    if dsn and dsn_password(dsn):
        fail(
            "--dsn carries a password, and atlantide.toml is meant to be committed. "
            f"Pass the dsn without it and supply the full one via {DSN_ENV} "
            "(or use ~/.pgpass / PGPASSWORD), or omit --dsn entirely to read it all "
            f"from {DSN_ENV}"
        )


def _check_not_nested(target: Path, *, force: bool) -> None:
    """Refuse to scaffold *inside* an existing project.

    ``atlantide.toml`` is found by walking up, so a second one below an existing
    project shadows it for every command run from that directory down.

    A toml in ``target`` itself is an ordinary file collision, which
    :func:`_check_collisions` reports together with any other conflicting files.
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
    """Refuse when any file already exists, naming all of them at once."""
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
    """Render every scaffold file in memory, before any write."""
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

    Returning ``None`` keeps a repeated ``init --force`` from duplicating the block.
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

    Slugified because it lands in S3 bucket names, which are stricter than
    directory names.
    """
    return slugify(target.name) or _FALLBACK_PREFIX


def _write(plan: _Plan) -> None:
    """Create the directory and write every file."""
    plan.directory.mkdir(parents=True, exist_ok=True)
    for entry in plan.files:
        with entry.path.open("a" if entry.append else "w") as handle:
            handle.write(entry.content)


# -- post-write verification --------------------------------------------------


def _report(plan: _Plan, template: str, *, validate: bool, json_out: bool) -> None:
    """Say what was written, compile it, and name the next command (or emit JSON)."""
    if not json_out:
        _report_created(plan)
    if validate:
        _compile_check(plan.directory, json_out=json_out)
    if json_out:
        written = sorted(f.path.name for f in plan.files)
        emit_json({"directory": str(plan.directory), "template": template, "created": written})
        return
    _report_next(plan.directory)


def _compile_check(target: Path, *, json_out: bool) -> None:
    """Compile the config that was just written, through the engine `validate` uses.

    In-process rather than a subprocess: it keeps the typed ``Result``, and
    ``sys.argv[0]`` is not ``atlantide`` inside the PyInstaller binary. The engine
    is stateless (a memory backend, no lock, no credentials), so this is safe
    even when the project was scaffolded against s3 or postgres.

    A failure here means a shipped template is broken; the files stay on disk so
    the problem can be reported.
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
    """List the written files; runs before the compile check."""
    for entry in sorted(plan.files, key=lambda f: f.path.name):
        verb = "appended" if entry.append else "created"
        console.print(f"[green]{verb}[/]  {escape(str(entry.path))}")


def _report_next(target: Path) -> None:
    """Print the next command to run."""
    location = "" if target == Path.cwd() else f"cd {target} && "
    console.print(f"\n[dim]next:[/]  {escape(location)}atlantide plan")
