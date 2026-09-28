# atlantide.core

The dependency-free SDK surface: everything a config author or a provider author
touches. Resource types, the `Provider` ABC, stacks, refs, and the error
taxonomy live here.

Enforced leaf of the layer graph — `core` imports no sibling package, so a
provider can be written against it without pulling in the engine, state, or IR.

| Module | Purpose |
| --- | --- |
| `resource.py` | `Resource` base class and the per-evaluation registry that collects declared resources. |
| `fields.py` | Per-field mutability: `mutable()`, `immutable()`, `computed()`, `secret()`. Read by the diff. |
| `types.py` | `Ref`, `SecretRef`, `StackOutputRef`, `Transform`, the `UNSET` sentinel, and the `concat`/`interpolate`/`join` combinators. |
| `markers.py` | Codec between live handles and their `{"$ref": ...}` marker form. |
| `stack.py` | `Stack` namespaces (region, tags, name prefix) and `StackReference`. |
| `component.py` | L2 components: library-authored groups of resources with auto-namespaced children. |
| `provider.py` | The async CRUD interface every provider implements. |
| `registry.py` | Provider registry and semver compatibility checks for pinned provider versions. |
| `lifecycle.py` | Per-instance overrides: `prevent_destroy`, `create_before_destroy`, `ignore_changes`, `aliases`. |
| `node_id.py` | The `{stack}:{type}:{name}` node-id format — the one place it is built and parsed. |
| `inline.py` | Folds in-config cross-stack output references into ordinary `Ref` edges. |
| `actions.py` | The CREATE/UPDATE/REPLACE/DELETE/NOOP vocabulary shared by diff, policy, and rendering. |
| `context.py` | The execution context handed to provider CRUD calls. |
| `check.py` | Preflight check results, reported by `atlantide state check`. |
| `policy.py` | Policy value types (pure data; evaluation lives in `atlantide.policy`). |
| `errors.py` | `AtlantideError` and its subclasses. |
| `events.py` | `ApplyEvent`, the per-run event stream behind progress display and the audit log. |
| `plugin.py` | `ProviderPlugin`, the contract a third-party provider package implements, including the one-name rule (entry point, plugin, declared types and built provider share a name; `identity_errors` / `provider_error` check it). Not importable from config. |
| `logging.py` | Diagnostic logging: levelled, redacted, always on stderr. Not importable from config. |
| `tuning.py` | Concurrency and client-timeout numbers shared by providers, secrets and state. Not importable from config. |
| `config.py` | `Config`, the typed environment matrix: every environment and what differs between them. |
| `_config_types.py` | The type and value checks behind `config.py`. |
| `_describe.py` | Stable, address-free descriptions of config values for error messages. |
| `_tree.py` | Recursive walks over property-value trees, including canonical set and key ordering, and `handles_to_markers`, the handle-to-marker rebuild shared by `types.py` and `markers.py`. |
| `_component_prefix.py` | The active component child-name prefix, shared by `component.py` (which sets it) and `resource.py` (which applies it) so neither imports the other at runtime. |
