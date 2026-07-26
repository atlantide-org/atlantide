"""Starter projects `atlantide init` writes, as inline source strings.

Templates are string constants rather than files shipped as package data, and
that is a packaging decision rather than a stylistic one. ``atlantide.spec``
builds a PyInstaller onefile binary with ``datas=[]`` and relies on
``collect_submodules``, which collects *modules*, not data files. A template
directory would therefore need a hand-written ``datas`` entry, and getting it
wrong yields a working wheel, a green CI, and a released binary where ``init``
dies on a missing file — the release workflow only smoke-tests ``--version``, so
nothing would catch it. A constant inside this module is collected because the
module is.

The configs below are Atlas-lang: valid Python that the bounded interpreter
executes. Two traps a new template will hit, both enforced by
:mod:`atlantide.lang.validate` and both silent until it runs:

* no ``from __future__ import annotations`` — ``__future__`` is not an allowed
  import, which is why no example config carries the line house style otherwise
  wants;
* inputs are read as ``atlantide.input(name)``. The bare builtin ``input`` is
  rejected, and the checker's own hint recommends this exact form.

``tests/cli/test_init.py`` compiles every template through the real engine, so a
template that violates the subset fails the suite rather than a user's first run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atlantide.components.lock import toml_string
from atlantide.secrets import SecretsConfig
from atlantide.secrets.factory import KEYFILE
from atlantide.state import StateConfig
from atlantide.state.factory import LOCAL, POSTGRES, S3

#: Template names accepted by ``--template``.
MINIMAL = "minimal"
AWS = "aws"

#: The config module every template writes, and the key pointing at it.
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
    #: ``[inputs]`` keys the config reads; ``init`` supplies the values, so a
    #: scaffolded project runs without a ``-var`` on the very first command.
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

    atlantide validate
    atlantide plan
    atlantide apply

S3 bucket names are globally unique. `name_prefix` composes them as
{prefix}-{name}-{stack}, so the dev stack asks for `{prefix}-assets-dev`. If that
name is taken, change [inputs].name_prefix in atlantide.toml.
"""

from atlantide.core import Stack, output
from atlantide.policy import enforce
from atlantide.providers.aws import Region, S3Bucket, SqsQueue

# Plan-time policy, evaluated against the changeset before anything is applied.
# A violation blocks the apply, so nothing is created.
enforce("require-tags", keys=["env", "owner"])
enforce("deny-destroy-in-protected", stacks=["prod"])

# Set in atlantide.toml under [inputs]; override per run with `-var name=value`,
# or per environment with a [profile.<name>.inputs] table.
prefix = atlantide.input("name_prefix")  # noqa: F821

for env in ["dev", "prod"]:
    # region, name_prefix and tags are stack-scoped: everything in the body
    # inherits them, and the same logical names live in every stack without
    # colliding -- node ids are dev:aws.S3Bucket:assets, prod:aws.S3Bucket:assets.
    with Stack(
        env,
        region=Region.EuNorth1,
        name_prefix=prefix,
        tags={"env": env, "owner": "platform"},
    ):
        # `bucket` and `queue_name` are omitted on purpose: the stack's
        # name_prefix composes them, so one prefix renames every environment.
        assets = S3Bucket("assets", versioning=(env == "prod"))
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

#: Opening line of the block :func:`render_gitignore` writes. Also how ``init``
#: recognises its own earlier block, so appending twice is a no-op.
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

    Only non-default tables are emitted: a local-state, keyfile-secrets project
    gets a file with nothing in it to explain. ``state`` and ``secrets`` have
    already been through their own ``validate()`` by the time this runs, so
    nothing here re-checks which keys a backend requires — see
    :mod:`atlantide.state.factory` for that list.
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
    ]


def _settings(config: object, keys: tuple[str, ...]) -> list[str]:
    """``key = "value"`` for each named setting that has one.

    An unset key is left out rather than written empty: the parser treats absent
    and empty differently, and a blank value in a scaffolded file reads as a
    setting somebody meant to fill in.
    """
    return [f"{key} = {toml_string(value)}" for key in keys if (value := getattr(config, key))]


def _profile_hint() -> list[str]:
    """A commented `[profile.prod]` overlay, shown only for a local-state project.

    A project that already has remote state has seen the shape; one that does not
    is exactly who needs to know that promoting an environment is a table, not a
    second directory.
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
