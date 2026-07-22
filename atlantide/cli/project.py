"""Optional per-project defaults read from ``atlantide.toml``.

Sets a default config file, state db, secrets store, AWS connection, and
parallelism. Explicit CLI arguments win; a missing file is not an error.

The file is looked up in the working directory and then in each parent, as git
and cargo do, so commands behave the same from anywhere inside a project. Without
that walk, running from a subdirectory silently drops the whole file — including
a remote ``[state]`` table, which turns a shared-state command into one against a
fresh empty local database. Every relative path in the file resolves against the
directory the file was found in (:attr:`ProjectConfig.root`), not the working
directory, so the same paths mean the same thing from either place.

Profiles overlay the top level, so one project can describe several
environments without duplicating a file per directory::

    state = "dev.db"

    [profile.prod]
    parallelism = 16

    [profile.prod.state]
    backend = "s3"
    bucket  = "acme-atlantide-state"
    key     = "prod/atlantide.json"

``atlantide --profile prod apply`` (or ``ATLANTIDE_PROFILE=prod``) merges
``[profile.prod]`` over the top-level keys, table by table.

Recognized keys (top level)::

    config        = "infra.py"          # default Atlas-lang config
    state         = "atlantide.db"      # default state database
    secrets_key   = "atlantide.key"     # secrets-store encryption keyfile
    secrets_store = "atlantide.secrets" # encrypted name->value store
    aws_region    = "eu-north-1"        # default AWS region
    aws_profile   = "prod"              # AWS shared-config profile
    aws_endpoint  = "http://localhost:4566"  # override endpoint (e.g. localstack)
    parallelism   = 16                  # max concurrent provider operations

Remote state — shared across machines, with cross-host per-subgraph locking. The
``state``/``--state`` file above is the local default; this table replaces it::

    [state]
    backend    = "s3"                   # "local" (default) | "s3" | "postgres"
    bucket     = "acme-atlantide-state" # s3: bucket holding the state object
    key        = "prod/atlantide.json"  # s3: object key
    lock_table = "atlantide-locks"      # s3: DynamoDB table holding the leases
    kms_key_id = "alias/atlantide"      # s3: optional SSE-KMS key (else AES256)
    region     = "eu-north-1"           # s3
    endpoint   = "http://localhost:4566"  # s3: optional (localstack)
    # backend = "postgres"
    # dsn    = "postgresql://..."       # or the ATLANTIDE_STATE_DSN env var
    # schema = "atlantide"

Secret resolution — which backend a ``SecretRef`` resolves against by default::

    [secrets]
    provider = "ssm"                    # "keyfile" (default) | "env" | "ssm"
    prefix   = "/atlantide/prod/"       # ssm: prepended to the secret name
    region   = "eu-north-1"             # ssm

Alternate accounts (a resource selects one via ``provider_alias=``)::

    [aws.aliases.prod]
    profile  = "prod-account"                # AWS shared-config profile
    endpoint = "http://localhost:4566"       # optional endpoint override

Published components fetched from public git repos (see
:mod:`atlantide.components`); config imports them as
``atlantide.components.<alias>``::

    [components.acme]
    git    = "https://github.com/acme/atlantide-secure-bucket"
    ref    = "v1.2.0"                         # tag/branch/commit requested
    subdir = "src"                            # optional: package location in the repo
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from atlantide.components.source import ComponentSource
from atlantide.core.errors import AtlantideError
from atlantide.secrets import SecretsConfig
from atlantide.state import StateConfig

# Re-exported: `[components.*]`, `[state]` and `[secrets]` are parsed here but the
# types live with the domain they configure.
__all__ = [
    "ComponentSource",
    "ProjectConfig",
    "ProjectError",
    "SecretsConfig",
    "StateConfig",
    "find_project_file",
    "load_project",
]


class ProjectError(AtlantideError):
    """``atlantide.toml`` asks for something it does not define (e.g. a profile)."""

_FILENAME = "atlantide.toml"


@dataclass(frozen=True)
class ProjectConfig:
    #: Directory ``atlantide.toml`` was found in; relative paths resolve against
    #: it. ``None`` when there is no file, in which case the cwd is the root.
    root: Path | None = None
    #: The ``[profile.<name>]`` overlay applied, if any.
    profile: str | None = None
    config: str | None = None
    state: str | None = None
    secrets_key: str | None = None
    secrets_store: str | None = None
    aws_region: str | None = None
    aws_profile: str | None = None
    aws_endpoint: str | None = None
    parallelism: int | None = None
    #: alias name -> {"profile": ..., "endpoint": ...} for alternate accounts.
    aws_aliases: dict[str, dict[str, str | None]] = field(default_factory=dict)
    #: alias -> git source for published components imported by config.
    components: dict[str, ComponentSource] = field(default_factory=dict)
    #: `[state]` — where state lives (local sqlite by default, or s3/postgres).
    state_backend: StateConfig = field(default_factory=StateConfig)
    #: `[secrets]` — which provider resolves secret values (keyfile by default).
    secrets: SecretsConfig = field(default_factory=SecretsConfig)

    @property
    def directory(self) -> Path:
        """The project root — the cwd when there is no ``atlantide.toml``."""
        return self.root if self.root is not None else Path.cwd()

    def resolve(self, path: str | Path) -> Path:
        """Anchor a project-relative path to the root, so it means the same thing
        whichever subdirectory the command ran from. Absolute paths pass through."""
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.directory / candidate


def find_project_file(start: Path | None = None) -> Path | None:
    """``atlantide.toml`` in ``start`` or the nearest ancestor holding one."""
    directory = (start or Path.cwd()).resolve()
    for candidate in (directory, *directory.parents):
        path = candidate / _FILENAME
        if path.is_file():
            return path
    return None


def load_project(start: Path | None = None, *, profile: str | None = None) -> ProjectConfig:
    """Read the nearest ``atlantide.toml`` at or above ``start`` (cwd by default).

    Returns an all-``None`` config when no file is found. ``profile`` names a
    ``[profile.<name>]`` table to overlay; naming one that does not exist is an
    error rather than a silent fall-through to the base config, since the whole
    point of asking for a profile is to not run against the other environment.
    """
    path = find_project_file(start)
    if path is None:
        return ProjectConfig(profile=profile)
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    data = _apply_profile(data, profile, path)

    def _str(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    parallelism = data.get("parallelism")
    return ProjectConfig(
        root=path.parent,
        profile=profile,
        config=_str("config"),
        state=_str("state"),
        secrets_key=_str("secrets_key"),
        secrets_store=_str("secrets_store"),
        aws_region=_str("aws_region"),
        aws_profile=_str("aws_profile"),
        aws_endpoint=_str("aws_endpoint"),
        parallelism=parallelism if isinstance(parallelism, int) else None,
        aws_aliases=_aws_aliases(data),
        components=_components(data),
        state_backend=_state(data),
        secrets=_secrets(data),
    )


def _apply_profile(
    data: dict[str, object], profile: str | None, path: Path
) -> dict[str, object]:
    """Merge ``[profile.<name>]`` over the top level and drop the profile tables.

    The merge is one level deep per table: a profile's ``[profile.prod.state]``
    replaces the keys it names in ``[state]`` and leaves the rest, so an
    environment overrides a bucket without restating the region.
    """
    profiles = data.pop("profile", None)
    if profile is None:
        return data
    table = profiles.get(profile) if isinstance(profiles, dict) else None
    if not isinstance(table, dict):
        available = sorted(profiles) if isinstance(profiles, dict) else []
        known = f" (defined: {', '.join(available)})" if available else ""
        raise ProjectError(f"no [profile.{profile}] in {path}{known}")
    return _merge(data, table)


def _merge(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    """``overlay`` over ``base``, merging one level of nested tables."""
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _table(data: dict[str, object], name: str) -> dict[str, object]:
    """The ``[name]`` table, or an empty one when absent or not a table."""
    table = data.get(name)
    return table if isinstance(table, dict) else {}


def _key(table: dict[str, object], key: str) -> str | None:
    value = table.get(key)
    return value if isinstance(value, str) else None


def _state(data: dict[str, object]) -> StateConfig:
    """Parse the ``[state]`` table (remote backend selection and its connection)."""
    table = _table(data, "state")
    return StateConfig(
        backend=_key(table, "backend") or "local",
        bucket=_key(table, "bucket"),
        key=_key(table, "key"),
        lock_table=_key(table, "lock_table"),
        kms_key_id=_key(table, "kms_key_id"),
        region=_key(table, "region"),
        profile=_key(table, "profile"),
        endpoint=_key(table, "endpoint"),
        dsn=_key(table, "dsn"),
        schema=_key(table, "schema"),
    )


def _secrets(data: dict[str, object]) -> SecretsConfig:
    """Parse the ``[secrets]`` table (which provider resolves secret values)."""
    table = _table(data, "secrets")
    return SecretsConfig(
        provider=_key(table, "provider") or "keyfile",
        prefix=_key(table, "prefix") or "",
        region=_key(table, "region"),
        profile=_key(table, "profile"),
        endpoint=_key(table, "endpoint"),
    )


def _components(data: dict[str, object]) -> dict[str, ComponentSource]:
    """Parse the ``[components.<alias>]`` tables into ``{alias: ComponentSource}``.

    Entries without a string ``git`` are skipped (a source with no repo to fetch
    is meaningless).
    """
    tables = data.get("components")
    if not isinstance(tables, dict):
        return {}
    result: dict[str, ComponentSource] = {}
    for alias, body in tables.items():
        if not isinstance(body, dict):
            continue
        git = body.get("git")
        if not isinstance(git, str):
            continue
        ref = body.get("ref")
        subdir = body.get("subdir")
        result[alias] = ComponentSource(
            git=git,
            ref=ref if isinstance(ref, str) else None,
            subdir=subdir if isinstance(subdir, str) else None,
        )
    return result


def _aws_aliases(data: dict[str, object]) -> dict[str, dict[str, str | None]]:
    """Parse the ``[aws.aliases.<name>]`` tables into ``{name: {profile, endpoint}}``."""
    aws = data.get("aws")
    aliases = aws.get("aliases") if isinstance(aws, dict) else None
    if not isinstance(aliases, dict):
        return {}
    result: dict[str, dict[str, str | None]] = {}
    for name, body in aliases.items():
        if isinstance(body, dict):
            profile = body.get("profile")
            endpoint = body.get("endpoint")
            result[name] = {
                "profile": profile if isinstance(profile, str) else None,
                "endpoint": endpoint if isinstance(endpoint, str) else None,
            }
    return result
