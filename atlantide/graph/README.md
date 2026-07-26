# atlantide.graph

The dependency DAG over IR nodes: construction, cycle rejection, deterministic
ordering, and the async scheduler that runs work across it.

| Module | Purpose |
| --- | --- |
| `model.py` | `DiGraph` — node ids plus their dependency edges. |
| `build.py` | Builds a `DiGraph` from IR and rejects cycles (iterative Tarjan; reports every cycle found). |
| `order.py` | Deterministic topological order via Kahn's algorithm. |
| `schedule.py` | Runs per-node coroutines respecting dependency order, bounded by a parallelism semaphore. Supports reverse order for deletes. |

Dependencies are awaited before the concurrency semaphore is acquired, so a low
parallelism setting cannot deadlock a deep graph.
