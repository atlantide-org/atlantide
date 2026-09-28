"""Optional per-project defaults read from ``atlantide.toml``.

Explicit CLI arguments win; a missing file is not an error. The file is looked
up in the working directory and then in each parent, as git and cargo do, so a
command run from a subdirectory still finds it; without it, a remote ``[state]``
table would be missed and the command would use a fresh local database.
Relative paths in the file resolve against the directory it was found in
(:attr:`ProjectConfig.root`), not the working directory.

``atlantide --profile prod`` (or ``ATLANTIDE_PROFILE=prod``) merges
``[profile.prod]`` over the top-level keys, table by table.

Every recognized key is listed in ``atlantide/cli/README.md``, under
"atlantide.toml keys".
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from atlantide.components.source import ComponentSource
from atlantide.core.errors import AtlantideError
from atlantide.lang import DEFAULT_FUEL
from atlantide.secrets import SecretsConfig
from atlantide.secrets.factory import parse_env_allow
from atlantide.state import StateConfig
from atlantide.util.project import PROJECT_FILENAME, find_project_file

# Re-exported: `[components.*]`, `[state]` and `[secrets]` are parsed here but the
# types live with the domain they configure; the project file's name and lookup
# are shared with the local provider, which may not import the CLI.
__all__ = [
    "MAX_FUEL",
    "PROJECT_FILENAME",
    "AwsAliasSettings",
    "ComponentSource",
    "ProjectConfig",
    "ProjectError",
    "SecretsConfig",
    "StateConfig",
    "check_fuel",
    "find_project_file",
    "load_project",
]


class AwsAliasSettings(TypedDict):
    """One ``[aws.aliases.<name>]`` table: an alternate account to reach."""

    profile: str | None
    endpoint: str | None


class ProjectError(AtlantideError):
    """``atlantide.toml`` asks for something it does not define (e.g. a profile)."""


#: Ceiling on ``[lang] fuel`` and ``--fuel``. This budget already allows minutes of
#: CPU, so a larger value is rejected as a likely typo.
MAX_FUEL = 100_000_000


@dataclass(frozen=True, slots=True)
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
    #: alias name -> its ``[aws.aliases.<name>]`` table, for alternate accounts.
    aws_aliases: dict[str, AwsAliasSettings] = field(default_factory=dict)
    #: alias -> git source for published components imported by config.
    components: dict[str, ComponentSource] = field(default_factory=dict)
    #: ``[inputs]``: per-project values ``atlantide.input()`` reads; overridden by
    #: ``--var-file``, then by ``--var``.
    inputs: dict[str, object] = field(default_factory=dict)
    #: ``[state]``: where state lives (local sqlite by default, or s3/postgres).
    state_backend: StateConfig = field(default_factory=StateConfig)
    #: ``[secrets]``: which provider resolves secret values (keyfile by default).
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    #: ``[provider.<name>]``: each provider's settings table, passed to its plugin
    #: factory (see :mod:`atlantide.core.plugin`).
    provider_tables: dict[str, dict[str, object]] = field(default_factory=dict)
    #: ``[lang] fuel``: the evaluation step budget; ``--fuel`` overrides it.
    fuel: int = DEFAULT_FUEL

    @property
    def directory(self) -> Path:
        """The project root, or the cwd when there is no ``atlantide.toml``."""
        return self.root if self.root is not None else Path.cwd()

    def resolve(self, path: str | Path) -> Path:
        """Anchor a project-relative path to the project root.

        The result does not depend on the subdirectory the command ran from.
        Absolute paths pass through.
        """
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.directory / candidate


def load_project(start: Path | None = None, *, profile: str | None = None) -> ProjectConfig:
    """Read the nearest ``atlantide.toml`` at or above ``start`` (cwd by default).

    Returns an all-``None`` config when no file is found. ``profile`` names a
    ``[profile.<name>]`` table to overlay; naming one that does not exist is an
    error rather than a fall-through to the base config, which may target a
    different environment.
    """
    path = find_project_file(start)
    if path is None:
        return ProjectConfig(profile=profile)
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            # Typed error: every command loads this file, and a traceback would
            # break --json output.
            raise AtlantideError(f"cannot parse {path}: {exc}") from exc
    data = _apply_profile(data, profile, path)
    parallelism = data.get("parallelism")
    return ProjectConfig(
        root=path.parent,
        profile=profile,
        config=_opt_str(data, "config"),
        state=_opt_str(data, "state"),
        secrets_key=_opt_str(data, "secrets_key"),
        secrets_store=_opt_str(data, "secrets_store"),
        aws_region=_opt_str(data, "aws_region"),
        aws_profile=_opt_str(data, "aws_profile"),
        aws_endpoint=_opt_str(data, "aws_endpoint"),
        parallelism=parallelism if isinstance(parallelism, int) else None,
        aws_aliases=_aws_aliases(data),
        components=_components(data),
        inputs=dict(_table(data, "inputs")),
        state_backend=_state(data),
        secrets=_secrets(data),
        provider_tables={
            name: dict(table)
            for name, table in _table(data, "provider").items()
            if isinstance(table, dict)
        },
        fuel=_fuel(data, path),
    )


def _apply_profile(data: dict[str, object], profile: str | None, path: Path) -> dict[str, object]:
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


def _opt_str(table: dict[str, object], key: str) -> str | None:
    """``table[key]`` when it is a string, else ``None`` (absent or mistyped)."""
    value = table.get(key)
    return value if isinstance(value, str) else None


def _seconds(table: dict[str, object], key: str) -> float | None:
    """A duration in seconds; accepts an int, since TOML parses ``300`` as one."""
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def check_fuel(value: object, where: str) -> int:
    """``value`` as a fuel budget: a positive int no larger than :data:`MAX_FUEL`.

    Out-of-range values are refused, not clamped, since clamping would change the
    evaluation budget.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_FUEL:
        raise ProjectError(f"{where} must be an integer between 1 and {MAX_FUEL:_}, got {value!r}")
    return value


def _fuel(data: dict[str, object], path: Path) -> int:
    """Parse ``[lang] fuel``; absent means :data:`~atlantide.lang.DEFAULT_FUEL`."""
    table = _table(data, "lang")
    if "fuel" not in table:
        return DEFAULT_FUEL
    return check_fuel(table["fuel"], f"[lang] fuel in {path}")


def _state(data: dict[str, object]) -> StateConfig:
    """Parse the ``[state]`` table (remote backend selection and its connection)."""
    table = _table(data, "state")
    return StateConfig(
        backend=_opt_str(table, "backend") or "local",
        bucket=_opt_str(table, "bucket"),
        key=_opt_str(table, "key"),
        lock_table=_opt_str(table, "lock_table"),
        kms_key_id=_opt_str(table, "kms_key_id"),
        region=_opt_str(table, "region"),
        profile=_opt_str(table, "profile"),
        endpoint=_opt_str(table, "endpoint"),
        dsn=_opt_str(table, "dsn"),
        schema=_opt_str(table, "schema"),
        lock_ttl=_seconds(table, "lock_ttl"),
        lock_renew_interval=_seconds(table, "lock_renew_interval"),
        node_timeout=_seconds(table, "node_timeout"),
        lock_skew_margin=_seconds(table, "lock_skew_margin"),
        journal_table=_opt_str(table, "journal_table"),
        write_concurrency=_positive_int(table, "write_concurrency"),
    )


def _positive_int(table: dict[str, object], key: str) -> int | None:
    """A positive integer setting; anything else is refused, naming the key."""
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProjectError(f"[state].{key} must be a positive integer, got {value!r}")
    return value


def _secrets(data: dict[str, object]) -> SecretsConfig:
    """Parse the ``[secrets]`` table (which provider resolves secret values)."""
    table = _table(data, "secrets")
    return SecretsConfig(
        provider=_opt_str(table, "provider") or "keyfile",
        prefix=_opt_str(table, "prefix") or "",
        region=_opt_str(table, "region"),
        profile=_opt_str(table, "profile"),
        endpoint=_opt_str(table, "endpoint"),
        env_allow=parse_env_allow(_table(table, "env").get("allow")),
    )


def _components(data: dict[str, object]) -> dict[str, ComponentSource]:
    """Parse the ``[components.<alias>]`` tables into ``{alias: ComponentSource}``.

    Entries without a string ``git`` are skipped.
    """
    return {
        alias: ComponentSource(git=git, ref=_opt_str(body, "ref"), subdir=_opt_str(body, "subdir"))
        for alias, body in _table(data, "components").items()
        if isinstance(body, dict) and (git := _opt_str(body, "git")) is not None
    }


def _aws_aliases(data: dict[str, object]) -> dict[str, AwsAliasSettings]:
    """Parse the ``[aws.aliases.<name>]`` tables into ``{name: {profile, endpoint}}``."""
    aliases = _table(_table(data, "aws"), "aliases")
    return {
        name: {"profile": _opt_str(body, "profile"), "endpoint": _opt_str(body, "endpoint")}
        for name, body in aliases.items()
        if isinstance(body, dict)
    }
