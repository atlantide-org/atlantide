"""Starter projects that ``atlantide init`` writes, as inline source strings.

Templates are string constants rather than package data: ``atlantide.spec`` builds a
PyInstaller onefile binary with ``datas=[]`` and ``collect_submodules``, which
collects modules but not data files. A template directory would need a separate
``datas`` entry, and an error there surfaces only in the released binary.

The configs below are Atlas-lang: valid Python that the bounded interpreter
executes. :mod:`atlantide.lang.validate` enforces two constraints on them:

* no ``from __future__ import annotations``: ``__future__`` is not an allowed import;
* inputs are read as ``atlantide.input(name)``; the bare builtin ``input`` is rejected.

``tests/cli/test_init.py`` compiles every template through the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atlantide.components.lock import toml_string
from atlantide.secrets import SecretsConfig
from atlantide.secrets.factory import ENV, KEYFILE
from atlantide.state import StateConfig
from atlantide.state.factory import LOCAL, POSTGRES, S3

#: Template names accepted by ``--template``.
MINIMAL = "minimal"
AWS = "aws"

#: The config module every template writes; the toml's ``config`` key points at it.
CONFIG_FILENAME = "infra.py"
#: Default local state database, matching the name the ``.gitignore`` block covers.
STATE_FILENAME = "atlantide.db"


@dataclass(frozen=True, slots=True)
class Template:
    """One starter project: a config module plus the inputs it expects."""

    name: str
    summary: str
    #: The Atlas-lang source written to :data:`CONFIG_FILENAME`.
    config: str
    #: ``[inputs]`` keys the config reads. ``init`` supplies the values, so the
    #: first command needs no ``-var``.
    inputs: tuple[str, ...] = field(default=())


_MINIMAL_CONFIG = '''\
"""Your first Atlantide config.

Valid Python -- editors, formatters and type checkers read it -- but executed by
Atlas-lang, a bounded interpreter with no clock, randomness, environment or
network. The same config always produces the same plan, and the engine relies on
that: an unchanged config hashes identically, so re-applying calls no provider.

    atlantide validate     # syntax, the language subset, and the graph
    atlantide plan         # what would change
    atlantide apply        # reconcile
    atlantide plan         # again: no changes
    atlantide destroy

`atlantide resources` lists every type this install can see, and
`atlantide schema local.File` prints one type's fields.

This starter uses the `local` provider, so it needs no cloud credentials.
Run `atlantide init --template aws` for an AWS starter instead.
"""

from atlantide.core import Stack, output
from atlantide.providers.local import File

# A Stack scopes region, tags and name_prefix over everything in its body. The
# local File has no region field and simply ignores it.
with Stack("dev", region="eu-north-1", tags={"env": "dev"}):
    greeting = File(
        "greeting",
        path="build/hello.txt",
        content="hello from atlantide\\n",
    )

    # Reading a computed field returns a lazy reference rather than a value. That
    # is what wires a dependency edge -- no depends_on, no string addresses -- and
    # it resolves to the real checksum at apply.
    output("greeting_checksum", greeting.checksum)
'''


_AWS_CONFIG = '''\
"""An AWS starter: a bucket and a queue per environment, with policy.

Valid Python -- editors, formatters and type checkers read it -- but executed by
Atlas-lang, a bounded interpreter with no clock, randomness, environment or
network. `validate` needs no credentials; `plan` and `apply` do.

    atlantide validate            # checks every environment
    atlantide plan
    atlantide apply
    atlantide apply --env prod    # prod only; dev is not diffed or touched

S3 bucket names are globally unique. `name_prefix` composes them as
{prefix}-{name}-{stack}, so the dev stack asks for `{prefix}-assets-dev`. If that
name is taken, change [inputs].name_prefix in atlantide.toml.
"""

from atlantide.core import Config, EnvSchema, Stack, output
from atlantide.policy import enforce
from atlantide.providers.aws import Region, S3Bucket, SqsQueue

# Plan-time policy, evaluated against the changeset before anything is applied.
# A violation blocks the apply, so nothing is created.
enforce("require-tags", keys=["env", "owner"])
enforce("deny-destroy-in-protected", stacks=["prod"])

# A per-run value from outside the repository -- this one differs per checkout,
# because S3 bucket names are globally unique. Set in atlantide.toml under
# [inputs]; override per run with `-var name=value`.
prefix = atlantide.input("name_prefix")  # noqa: F821


class AppEnv(EnvSchema):
    """What differs between this project's environments.

    The one class Atlas-lang admits: annotated fields only, no methods and no
    decorators, so it is data. Declaring it is what lets an editor complete
    `env.versioning` below and flag a misspelling of it.

    `region`, `tags` and `name_prefix` are well-known keys every environment
    carries, so they need no declaration here.
    """

    versioning: bool = False


# Every environment and what differs between them, declared once and typed: a
# missing or mistyped prod value fails `validate` rather than the prod apply.
config = Config(
    AppEnv,
    envs={
        "dev": {
            "region": Region.EuNorth1,
            "name_prefix": prefix,
            "tags": {"env": "dev", "owner": "platform"},
        },
        "prod": {
            "region": Region.EuNorth1,
            "name_prefix": prefix,
            "tags": {"env": "prod", "owner": "platform"},
            "versioning": True,
        },
    },
)

for env in config.envs():
    # region, name_prefix and tags are stack-scoped: everything in the body
    # inherits them, and the same logical names live in every stack without
    # colliding -- node ids are dev:aws.S3Bucket:assets, prod:aws.S3Bucket:assets.
    with Stack(env.name, config=env):
        # `bucket` and `queue_name` are omitted on purpose: the stack's
        # name_prefix composes them, so one prefix renames every environment.
        assets = S3Bucket("assets", versioning=env.versioning)
        jobs = SqsQueue("jobs")

        # Computed fields are lazy references -- the dependency edge, resolved at
        # apply. Read them from another resource to wire the graph.
        output("assets_arn", assets.arn)
        output("jobs_url", jobs.url)
'''


TEMPLATES: dict[str, Template] = {
    MINIMAL: Template(
        name=MINIMAL,
        summary="One local file. No cloud credentials needed.",
        config=_MINIMAL_CONFIG,
    ),
    AWS: Template(
        name=AWS,
        summary="An S3 bucket and an SQS queue per environment, with policy.",
        config=_AWS_CONFIG,
        inputs=("name_prefix",),
    ),
}

#: Template names, ordered for `--template`'s diagnostics.
TEMPLATE_NAMES: tuple[str, ...] = tuple(sorted(TEMPLATES))


# -- .gitignore ---------------------------------------------------------------

#: Opening line of the block :func:`render_gitignore` writes. ``init`` also uses it
#: to detect an existing block, so a second append is a no-op.
GITIGNORE_MARKER = "# --- atlantide ---"

_GITIGNORE_BLOCK = f"""\
{GITIGNORE_MARKER}
# Derived, secret, or machine-local. None of it belongs in git.
# Annotations sit on their own lines: git has no inline comments, so a trailing
# `# ...` would make the whole line a literal, never-matching pattern.
# local sqlite state, plus its WAL/shm files (the backend runs in WAL mode)
{STATE_FILENAME}
{STATE_FILENAME}-wal
{STATE_FILENAME}-shm
# secrets-store encryption key -- never commit this
atlantide.key
# encrypted name -> value store
atlantide.secrets
# vendored components; rebuild with `atlantide component vendor`
.atlantis/
# built artifacts (`atlantide build`)
*.atlas
# state snapshots (`atlantide state backup`)
*.atlas-state
# atlantide.lock is NOT ignored: it pins component commits and their hashes, and
# belongs in git the way any lockfile does.
# -----------------------------------------------------------------------------
"""


def render_gitignore() -> str:
    """The atlantide block appended to (or written as) ``.gitignore``."""
    return _GITIGNORE_BLOCK


# -- atlantide.toml -----------------------------------------------------------


def render_toml(
    *,
    state: StateConfig,
    secrets: SecretsConfig,
    inputs: dict[str, str],
    aws_region: str | None = None,
) -> str:
    """Render ``atlantide.toml`` for a scaffolded project.

    Only non-default tables are emitted. ``state`` and ``secrets`` have already
    passed their own ``validate()``, so required keys are not re-checked here (see
    :mod:`atlantide.state.factory`).
    """
    lines = [
        "# Project defaults, found by walking up from the working directory, so every",
        "# command means the same thing from any subdirectory. Relative paths below",
        "# resolve against THIS file's directory, not the one you are standing in.",
        f"config = {toml_string(CONFIG_FILENAME)}",
    ]
    if state.backend == LOCAL:
        lines.append(f"state  = {toml_string(STATE_FILENAME)}")
    if aws_region:
        lines.append(f"aws_region = {toml_string(aws_region)}")
    if inputs:
        lines += [
            "",
            "# Values the config reads with `atlantide.input(name)`. Override per run",
            "# with `-var name=value`, or per environment with [profile.<name>.inputs].",
            "[inputs]",
            *(f"{key} = {toml_string(value)}" for key, value in sorted(inputs.items())),
        ]
    lines += _state_table(state)
    lines += _secrets_table(secrets)
    if state.backend == LOCAL:
        lines += _profile_hint()
    return "\n".join(lines) + "\n"


def _state_table(state: StateConfig) -> list[str]:
    """The ``[state]`` table, or nothing at all for the local default."""
    if state.backend == LOCAL:
        return []
    keys = (
        ("bucket", "key", "lock_table", "kms_key_id", "region", "profile", "endpoint")
        if state.backend == S3
        else ("dsn", "schema")
    )
    lines = [
        "",
        "# Remote state: shared across machines, with cross-host per-subgraph locking.",
        "# It replaces the local `state` file above rather than supplementing it.",
        "[state]",
        f"backend = {toml_string(state.backend)}",
        *_settings(state, keys),
    ]
    if state.backend == POSTGRES and not state.dsn:
        lines.append("# dsn comes from the ATLANTIDE_STATE_DSN environment variable.")
    return lines


def _secrets_table(secrets: SecretsConfig) -> list[str]:
    """The ``[secrets]`` table, or nothing at all for the keyfile default."""
    if secrets.provider == KEYFILE:
        return []
    return [
        "",
        "# Which backend a SecretRef resolves against by default.",
        "[secrets]",
        f"provider = {toml_string(secrets.provider)}",
        *_settings(secrets, ("prefix", "region", "profile", "endpoint")),
        *(_env_allow_table(secrets) if secrets.provider == ENV else ()),
    ]


def _env_allow_table(secrets: SecretsConfig) -> list[str]:
    """``[secrets.env]``: the env provider reads only allow-listed names."""
    patterns = ", ".join(toml_string(pattern) for pattern in secrets.env_allow)
    return [
        "",
        '# Env vars the env provider may read (case-sensitive globs, e.g. "APP_*").',
        "# Deny by default: a name matching no pattern is refused.",
        "[secrets.env]",
        f"allow = [{patterns}]",
    ]


def _settings(config: object, keys: tuple[str, ...]) -> list[str]:
    """``key = "value"`` for each named setting that has one.

    An unset key is omitted rather than written empty: the parser treats absent
    and empty differently.
    """
    return [f"{key} = {toml_string(value)}" for key in keys if (value := getattr(config, key))]


def _profile_hint() -> list[str]:
    """A commented `[profile.prod]` overlay, shown only for a local-state project.

    It shows that promoting an environment takes a profile table, not a second
    directory.
    """
    return [
        "",
        "# A profile overlays the top level, table by table:",
        "#     atlantide --profile prod plan",
        "# [profile.prod]",
        "# parallelism = 16",
        "# [profile.prod.state]",
        '# backend    = "s3"',
        '# bucket     = "acme-atlantide-state"',
        '# key        = "prod/atlantide.json"',
        '# lock_table = "atlantide-locks"',
    ]
