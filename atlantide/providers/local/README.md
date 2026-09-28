# atlantide.providers.local

Local-disk resources. Useful for exercising the full engine — graph, state,
refresh, rollback — with no cloud account, and used throughout the test suite.

| Module | Purpose |
| --- | --- |
| `resources.py` | `File` (managed content, computed `checksum`), `SourceFile` (read-only; its sha256 is an *input*, so a changed file drives an UPDATE), `Null` (no-op, useful as a graph edge or a trigger). |
| `provider.py` | `LocalProvider`: disk CRUD for `File`, reads for `SourceFile`, no-ops for `Null`. |
| `paths.py` | `PathScope`: relative paths resolve against the project root (the `atlantide.toml` directory, not the cwd) and must stay inside it; `[provider.local] allow_outside_project = true` opts out. No project file → the working directory (captured when the provider is built) is the root, confined the same way; opt out with `LocalProvider(allow_outside_project=True)` or the settings key. `SourceFile` fingerprints at eval time under the same rule. |
