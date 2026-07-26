# atlantide.providers

Providers: the async CRUD implementations behind each family of resource types,
and the typed resource classes config declares.

Every provider depends only on `atlantide.core` (enforced by an import
contract), so a provider can be developed and tested without the engine.

Third-party providers are ordinary packages: implement the ABC, expose a
`ProviderPlugin` (see `atlantide.core.plugin`), and advertise it in the
`atlantide.providers` entry-point group. The three here are discovered through
that same group — `loader.py` hardcodes nothing — so the path a third party takes
is the one exercised on every run. `loader.py` collects load failures rather than
raising them: a broken plugin must not stop the commands used to diagnose it.

| Package | Contents |
| --- | --- |
| `aws/` | The AWS provider: resource types, per-service CRUD handlers, IAM policy builders, L2 components. |
| `local/` | `File`, `SourceFile`, and `Null` — disk CRUD, useful for tests and for wiring a graph without a cloud account. |
| `random/` | Values generated once at apply and pinned in state (ids, passwords, suffixes). |

A provider declares a registry `name` and a semver `version`. The version is
stamped into every IR node at lowering, pinned in an artifact, and
compatibility-checked before apply.

Config imports resource *types* from these packages; the `Provider` classes
themselves are registered and driven by the CLI and are not importable from
Atlas-lang.
