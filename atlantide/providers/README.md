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

A plugin is **one name throughout**. The entry-point key, `ProviderPlugin.name`,
the `Meta.provider` of every type in `types` (each keyed by its own
`type_name()`), and the `name` of the provider `factory` builds must all be the
same string:

```toml
[project.entry-points."atlantide.providers"]
acme = "acme_atlantide:PLUGIN"   # key == PLUGIN.name == AcmeProvider.name == "acme"
```

Resources are routed to a provider by that name and state rows are resolved
through the type key, so a plugin that could build a provider, or declare a
type, under another name could handle resources that are not its own —
`aws`'s, say, on a machine where `aws` is not installed. Discovery checks the
declarations before any factory runs; the CLI checks the built provider's name
before registering it. Two installed plugins claiming one name are refused too,
rather than resolved by whichever distribution the metadata lists first. Any of
these refusals aborts every command that builds providers with
`provider plugin 'X' could not be registered: …` (a `RegistryError` envelope
under `--json`). `atlantide providers` lists the plugin as `refused`
(`"fatal": true` in its JSON) and exits 1; the `state` commands are unaffected,
and `--no-plugins` runs with the built-ins alone. A plugin that merely fails to
*load* stays a warning: it contributes nothing, so a run that does not need it
can proceed.

| Module or package | Contents |
| --- | --- |
| `loader.py` | Entry-point discovery of provider plugins, collecting load failures instead of raising, and refusing a plugin that is not one name throughout. |
| `aws/` | The AWS provider: resource types, per-service CRUD handlers, IAM policy builders, L2 components. |
| `local/` | `File`, `SourceFile`, and `Null` — disk CRUD, useful for tests and for wiring a graph without a cloud account. |
| `random/` | Values generated once at apply and pinned in state (ids, passwords, suffixes). |

A provider declares a registry `name` and a semver `version`. The version is
stamped into every IR node at lowering, pinned in an artifact, and
compatibility-checked before apply.

Config imports resource *types* from these packages; the `Provider` classes
themselves are registered and driven by the CLI and are not importable from
Atlas-lang.
