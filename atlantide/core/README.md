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
| `_tree.py` | Recursive walks over property-value trees, including canonical set and key ordering. |
