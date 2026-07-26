# atlantide.reconcile

Desired IR against current state: what changes, in what order, and the execution
of those changes against real providers.

| Module | Purpose |
| --- | --- |
| `diff.py` | Classifies each node id into CREATE / UPDATE / REPLACE / DELETE / NOOP. Comparison is symbolic (properties keep their `$ref` markers), matching the Merkle hash. |
| `planner.py` | Enforces the `prevent_destroy` guard over a ChangeSet. |
| `executor.py` | Runs the ChangeSet: applies forward over the desired graph, deletes in reverse over the prior-state graph, persists state per node, and compensates on failure under `on_failure="rollback"`. |
| `refresh.py` | Reads live provider state and reports drift; `write=True` folds the result back into state. |
| `resolve.py` | Resolves `Ref`, `SecretRef`, `StackOutputRef`, and `$transform` handles to values, and rebuilds a live `Resource` from a stored node. |
| `adopt.py` | Import: reads an already-existing resource through its provider, checks it against config, and writes the state row an apply would have written — so the next plan is a NOOP rather than a CREATE. Calls no mutating provider method. |
| `aliases.py` | Rename-without-replace: rekeys prior state from an old node id to a new one and re-hashes affected dependents. |
| `context.py` | Shared apply/refresh plumbing: `ApplyEnv`, `Desired`, progress callbacks, phase constants. |

The Merkle `input_hash` is a function of config alone, so two channels carry a
state-side change into the diff: `NO_INPUT_HASH`, written by `refresh --write`
when live inputs drift, and `_stale_dependents`, which pulls a node out of NOOP
when an upstream node is being recreated under it.
