# atlantide.graph

The dependency DAG over IR nodes: construction, cycle rejection, deterministic
ordering, and the async scheduler that runs work across it.

| Module | Purpose |
| --- | --- |
| `model.py` | `DiGraph` — node ids plus their dependency edges. |
| `build.py` | Builds a `DiGraph` from IR and rejects cycles (iterative Tarjan; reports every cycle found). |
| `cbd.py` | Create-before-destroy propagation: a declared node makes its dependency closure create-before-destroy too (`effective_cbd`, used by the diff and the apply's lock scope). |
| `order.py` | Deterministic topological order via Kahn's algorithm. |
| `select.py` | `--target` selection: `match_targets` resolves ids, short forms and globs to node ids; `closure` widens a selection to its dependencies (create/update) or its dependents (destroy). |
| `schedule.py` | Runs per-node coroutines respecting dependency order, bounded by a parallelism semaphore. Supports reverse order for deletes. |

Dependencies are awaited before the concurrency semaphore is acquired, so a low
parallelism setting cannot deadlock a deep graph.
