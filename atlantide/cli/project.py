"""Optional per-project defaults read from ``atlantide.toml``.

Sets a default config file, state db, secrets store, AWS connection, and
parallelism. Explicit CLI arguments win; a missing file is not an error.

Recognized keys (top level)::

    config        = "infra.py"          # default Atlas-lang config
    state         = "atlantide.db"      # default state database
    secrets_key   = "atlantide.key"     # secrets-store encryption keyfile
    secrets_store = "atlantide.secrets" # encrypted name->value store
    aws_region    = "eu-north-1"        # default AWS region
    aws_profile   = "prod"              # AWS shared-config profile
    aws_endpoint  = "http://localhost:4566"  # override endpoint (e.g. localstack)
    parallelism   = 16                  # max concurrent provider operations

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

# Re-exported: `[components.*]` sources are parsed here but the type lives with the
# rest of the components domain.
__all__ = ["ComponentSource", "ProjectConfig", "load_project"]

_FILENAME = "atlantide.toml"


@dataclass(frozen=True)
class ProjectConfig:
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


def load_project(start: Path | None = None) -> ProjectConfig:
    """Read ``atlantide.toml`` from ``start`` (cwd by default).

    Returns an all-``None`` config when the file is absent.
    """
    path = (start or Path.cwd()) / _FILENAME
    if not path.is_file():
        return ProjectConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    def _str(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    parallelism = data.get("parallelism")
    return ProjectConfig(
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
