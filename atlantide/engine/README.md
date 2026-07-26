# atlantide.engine

Orchestration: wires the pure stages (Atlas-lang → IR → graph → Merkle → diff)
to the effectful ones (executor, state backend), and takes the whole-state lock
around every mutation.

`Engine` is the library entrypoint; the CLI is a thin wrapper over it.

| Module | Purpose |
| --- | --- |
| `__init__.py` | The `Engine` class: `compile`, `plan`, `apply`, `destroy`, `refresh`, `build`, `deploy`. |
| `planner.py` | Plan refinement and policy evaluation: compiled config plus prior state produce a `Plan`. Also audits stored secret digests for rotation. |
| `hydrate.py` | Assembles a `Compiled` from IR, whether freshly lowered or rehydrated from a stored artifact. |
| `locking.py` | Lock-owner identity, lock scope, and the acquire/run/release shape. |
| `model.py` | `Compiled` and `Plan` value types. |

Two-tier error model: the pure and planning stages surface failure as
`Result[..., AtlantideError]` and compose with `.bind`/`.map`; the async
execution path raises, and those exceptions are collected into an
`ExceptionGroup` at the boundary. The two are never converted into each other.

An apply re-diffs against the state read *after* the lease is acquired, so a
plan computed before the lock is used only to size the lock scope.
