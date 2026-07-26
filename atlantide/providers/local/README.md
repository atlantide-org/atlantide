# atlantide.providers.local

Local-disk resources. Useful for exercising the full engine — graph, state,
refresh, rollback — with no cloud account, and used throughout the test suite.

| Module | Purpose |
| --- | --- |
| `resources.py` | `File` (managed content, computed `checksum`), `SourceFile` (read-only; its sha256 is an *input*, so a changed file drives an UPDATE), `Null` (no-op, useful as a graph edge or a trigger). |
| `provider.py` | `LocalProvider`: disk CRUD for `File`, reads for `SourceFile`, no-ops for `Null`. |
