# atlantide.cli

The `atlantide` command line: a thin wrapper over `Engine` plus the rendering,
option, and error plumbing around it.

| Module | Purpose |
| --- | --- |
| `main.py` | The entry point (`atlantide.cli.main:main`); re-exports `app`. |
| `app.py` | The root Typer app: global flags (`--debug`, `--profile`, logging, audit log), rendering of uncaught `AtlantideError`s, and every command registered in `--help` order. |
| `commands/run.py` | `plan`, `apply`, `destroy`, `refresh`. |
| `commands/validate.py` | `validate`: compile a config without state or providers. |
| `commands/providers.py` | `providers`: the plugins this install can see. |
| `commands/graph.py` | `graph`: the dependency graph as Mermaid or dot. |
| `commands/artifact.py` | `build`, `verify`, `deploy`: the `.atlas` artifact lifecycle. |
| `commands/imports.py` | `import`: adopt existing infrastructure into state. |
| `commands/outputs.py` | `output`: what a previous apply exported, read from state alone. |
| `commands/introspect.py` | `resources`, `schema`: resource-type introspection. |
| `commands/component.py` | The `component` group: `add`, `lock`, `vendor`, `verify`. |
| `commands/secrets.py` | The `secret` group: the local encrypted value store. |
| `commands/init/` | `init`: scaffolds a project, then compiles what it wrote. `templates.py` holds the starter projects as inline strings — package data would be missing from the PyInstaller binary and CI could not see it. |
| `commands/state/` | The `state` group: `check`, `backup`/`restore` (`snapshot.py`), `list`/`show`/`rm` (`nodes.py`), `migrate`, `unlock` (`locks.py`), `compact`/`fsck` (`journal.py`); `common.py` holds the target resolution and whole-state reads and writes they share. |
| `views/` | Rendering: each topic's Rich view beside its `--json` document (`plan`, `report`, `drift`, `imports`, `state`), the shared vocabulary (`common`), and how JSON reaches stdout (`output`). Commands keep flow control and exit codes. |
| `target.py` | Resolves the invocation's context: which profile, which project, which state backend (`resolve_target`). |
| `wiring.py` | Discovers provider plugins, mounts vendored components, and builds providers and engines. |
| `config_source.py` | The config a command was pointed at: located, read, and paired with its inputs (`ConfigRun`). |
| `project.py` | Per-project defaults read from `atlantide.toml`, including `[profile.<name>]` overlays; keys are listed [below](#atlantidetoml-keys). |
| `options.py` | Option types and confirmation prompts shared by more than one command. |
| `context.py` | The invocation's root flags and `--json` mode, as a context variable. |
| `progress.py` | Live per-node progress table for apply, deploy, and destroy. |
| `audit.py` | The `--audit-log` JSONL sink, its run header, and the logging mirror of the event stream. |
| `diagram.py` | Graphviz dot and Mermaid export for `graph`. |
| `errors.py` | Async-run bridging, `ExceptionGroup` flattening, diagnostics, exit codes. |
| `console.py` | The shared Rich consoles (stdout, and stderr under `--json`). |

Every command that touches state announces which state it is: with a shared
backend, "no changes" and "wrong target" are otherwise indistinguishable until
something is destroyed. Mutating commands require confirmation, and sensitive
values are redacted at this boundary.

Set `ATLANTIDE_DEBUG=1` (or pass `--debug`) to print the full traceback and
cause chain on error.

## atlantide.toml keys

Parsed by `project.py`.

Profiles overlay the top level, so one project can describe several
environments without duplicating a file per directory:

```toml
state = "dev.db"

[profile.prod]
parallelism = 16

[profile.prod.state]
backend = "s3"
bucket  = "acme-atlantide-state"
key     = "prod/atlantide.json"
```

`atlantide --profile prod apply` (or `ATLANTIDE_PROFILE=prod`) merges
`[profile.prod]` over the top-level keys, table by table.

Recognized keys (top level):

```toml
config        = "infra.py"          # default Atlas-lang config
state         = "atlantide.db"      # default state database
secrets_key   = "atlantide.key"     # secrets-store encryption keyfile
secrets_store = "atlantide.secrets" # encrypted name->value store
aws_region    = "eu-north-1"        # default AWS region
aws_profile   = "prod"              # AWS shared-config profile
aws_endpoint  = "http://localhost:4566"  # send every AWS call here instead
parallelism   = 16                  # max concurrent provider operations
```

Remote state — shared across machines, with cross-host per-subgraph locking. The
`state`/`--state` file above is the local default; this table replaces it:

```toml
[state]
backend    = "s3"                   # "local" (default) | "s3" | "postgres"
bucket     = "acme-atlantide-state" # s3: bucket holding the state object
key        = "prod/atlantide.json"  # s3: object key
lock_table = "atlantide-locks"      # s3: DynamoDB table holding the leases
journal_table = "atlantide-heads"   # s3: optional table for journal heads (default: lock_table)
write_concurrency = 16              # s3: state writes in flight at once (<= parallelism)
kms_key_id = "alias/atlantide"      # s3: optional SSE-KMS key (else AES256)
region     = "eu-north-1"           # s3
endpoint   = "http://localhost:4566"  # s3: send state calls here instead
# backend = "postgres"
# dsn    = "postgresql://..."       # or the ATLANTIDE_STATE_DSN env var
# schema = "atlantide"
lock_ttl            = 300           # seconds a lease lasts before it lapses
lock_renew_interval = 100           # how often a live run pushes that out
node_timeout        = 2400          # ceiling on one resource's reconcile
lock_skew_margin    = 30            # s3/postgres: grace before a lapsed lease is taken
```

The lease is renewed for as long as a run is alive, so `lock_ttl` bounds how
long a *dead* run blocks its teammates rather than how long a live one may take —
a lower value is a faster recovery, not a shorter deadline. The interval must be
well under the TTL so a single slow renewal is survivable.

An expired S3 or Postgres lease is only taken over once it is
`lock_skew_margin` seconds past its expiry. On S3 expiry is judged by the
taker's clock, so a host whose clock runs up to that much fast cannot take a live
lease from a healthy run; Postgres judges it by the database server's clock, and
the margin gives a late heartbeat the same grace. It also delays reclaiming a
dead run's lease by the same amount.

Secret resolution — which backend a `SecretRef` resolves against by default:

```toml
[secrets]
provider = "ssm"                    # "keyfile" (default) | "env" | "ssm"
prefix   = "/atlantide/prod/"       # ssm: prepended to the secret name
region   = "eu-north-1"             # ssm

[secrets.env]
allow = ["APP_*", "DB_PASSWORD"]    # env vars the env provider may read;
                                    # empty/absent = none (deny by default)
```

Alternate accounts (a resource selects one via `provider_alias=`):

```toml
[aws.aliases.prod]
profile  = "prod-account"                # AWS shared-config profile
endpoint = "http://localhost:4566"       # optional endpoint override
```

Per-run inputs the config reads with `atlantide.input(name)`:

```toml
[inputs]
instance_count = 2

[profile.prod.inputs]
instance_count = 10
```

`--var-file f.toml` overrides these, and `-var name=value` overrides both.

Environments are declared with `Config` (`atlantide.core.config`) in the
config file and selected with `--env`, not with inputs. The three mechanisms:

- `[inputs]` / `--var` — a per-run value from outside the repository (a CI
  build number, a fork's name prefix). Untyped, since it arrives as text.
- `Config(AppEnv, envs=...)` — the checked-in, typed environment matrix,
  validated when the config is evaluated. `AppEnv` is an `EnvSchema`
  subclass, so an editor completes each variable.
- `[profile.<name>]` — where the run points: state backend, AWS
  profile/region, parallelism, plus an `[inputs]` overlay.

`--profile` and `--env` are orthogonal; neither implies the other.

Atlas-lang evaluation — the step budget a config may spend (`--fuel` wins):

```toml
[lang]
fuel = 20_000_000   # default 5_000_000, ~100 steps per resource
```

Per-provider settings, handed to that provider's plugin:

```toml
[provider.local]
allow_outside_project = true   # let local paths leave the project root
```

Published components fetched from public git repos (see
`atlantide.components`); config imports them as
`atlantide.components.<alias>`:

```toml
[components.acme]
git    = "https://github.com/acme/atlantide-secure-bucket"
ref    = "v1.2.0"                         # tag/branch/commit requested
subdir = "src"                            # optional: package location in the repo
```
