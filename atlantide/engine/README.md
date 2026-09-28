# atlantide.engine

Orchestration: wires the pure stages (Atlas-lang → IR → graph → Merkle → diff)
to the effectful ones (executor, state backend), and takes the state lock
around every mutation.

`Engine` is the library entrypoint; the CLI is a thin wrapper over it.

| Module | Purpose |
| --- | --- |
| `engine.py` | The `Engine` façade: `compile`, `plan`, `apply`, `apply_compiled`, `destroy`, `refresh`, `build`, `deploy`, `import_nodes`. Each method delegates to the modules below. |
| `compiler.py` | Source → `Compiled` (evaluate, lower, hash) and artifact → `Compiled` (verify pins, rehydrate resources from IR). |
| `selection.py` | Pure node selection: `--target` closure, `--replace` matching, `--env` narrowing, targeted-destroy closure, import ordering. |
| `planner.py` | Plan refinement: compiled config plus prior state produce a `Plan`, running the post-diff passes in order (secret audit, undefined secrets and stack outputs, create-before-destroy collision resolution via `reconcile.ordering.resolve_cbd`, policy). Also builds the destroy changeset, and refuses an apply whose changes drifted from the approved plan. `protected_ids` decides which flag the `prevent_destroy` guard reads (see below). |
| `secret_audit.py` | `SecretAudit` / `audit_secrets`: compares the unchanged nodes' live secret values with their stored digests, upgrades a rotated NOOP to an UPDATE or REPLACE, and warns when the misses point at a foreign `secrets_key` rather than a rotation. |
| `policy_eval.py` | `evaluate_policies`: runs the config's and the resource classes' policy bindings over a plan's actionable changes. |
| `runs.py` | `LockedRuns`: the locked-run scaffold every mutation goes through — fresh lease guard, run id, state writer, re-read under the lease, post-run checkpoint, alias migration. |
| `locking.py` | Lock-owner identity, lock scope, the acquire/renew/run/release shape, and `require_no_new_nodes` (refuse when state gained rows while the lock was awaited). |
| `model.py` | `Compiled` and `Plan` value types. |
| `result.py` | The `Result` ↔ raise bridges: `catching`, `forward_failure`, `raise_on_failure`. |

## Error model

Two tiers, and they are never converted into each other except at the bridges
in `result.py`:

| Layer | Style |
| --- | --- |
| `lang.evaluate_source`, `graph.build_graph`, `ir.artifact.loads`/`verify_hash`, `core.registry.*`, `Engine.compile`/`plan`/`build`/`deploy`/`apply` | Return `Result[..., AtlantideError]`, composed with `.bind`/`.map` |
| `StateBackend.acquire_lock`/`renew_lock`/`release_lock` | Return `Result[..., LockError]` |
| State writes, secrets, policy, providers, the executor, the interpreter internals | Raise |
| `cli/errors.py` (`run_async`, `unwrap_or_*`) | The single boundary back to exit codes |

A raising helper called from a `Result` stage goes through `catching`; a
`Result` needed inside the lock, where nothing can be returned, goes through
`raise_on_failure`. The async execution path collects its exceptions into an
`ExceptionGroup` at the boundary.

The registries follow the layer they live in: `core.registry.ProviderRegistry`
returns `Result` (public API), while `secrets.registry.SecretsRegistry` raises
`SecretsError` and `policy.registry.PolicyRegistry` raises `RegistryError`.

## Locking

An apply re-diffs against the state read *after* the lease is acquired, so a
plan computed before the lock is used only to size the lock scope. Destroy and
`refresh --write` re-read the same way (see `LockedRuns.run_locked`).

## `prevent_destroy`

`protected_ids(prior, desired)` follows Terraform's model. For a node the config
declares, the config's flag decides: protection added in a plan already refuses a
replace in that plan, and protection removed in it already permits one. For a
node the config does not declare (a DELETE, or `destroy`, which has no config),
the flag recorded in state decides. A protect-only edit plans as a state-only
NOOP that the apply records without a provider call (see
`atlantide/reconcile/README.md`), and it counts as a pending change for
`--detailed-exitcode` and the apply prompt. A protected node whose REPLACE is
conditional is let through at plan time with a warning and judged at apply.
