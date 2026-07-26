# atlantide.state

The graph state store: one record per resource, plus committed stack outputs and
the per-node lock leases.

The engine talks only to `StateBackend`; concrete backends are interchangeable
behind it and selected declaratively through `make_state_backend`. The remote
backends stay out of the package namespace so their dependencies load only when
configured.

| Module | Purpose |
| --- | --- |
| `backend.py` | `StateNode` / `StateGraph` value types and the storage-agnostic `StateBackend` interface, including the lock lease model. |
| `codec.py` | Serialization shared by every persistent backend: canonical row columns for table stores, whole-document JSON for object stores. |
| `factory.py` | `StateConfig` → a configured backend. |
| `memory_backend.py` | In-process, volatile. Same semantics as sqlite. |
| `sqlite_backend.py` | Default: embedded SQLite in WAL mode, single file, ACID. |
| `postgres_backend.py` | Shared remote state, one row per node. Requires the `postgres` extra. |
| `s3_backend.py` | Shared remote state: one S3 object for the graph under an ETag compare-and-swap, DynamoDB rows for leases. |

Writes are incremental: a node's row lands the moment its provider call
succeeds, so a crash mid-apply leaves a consistent state that a re-run resumes
from. `replace_many` exists for moves (an alias rekey), which must never be
observed holding neither id.
