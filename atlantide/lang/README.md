# atlantide.lang

Atlas-lang: a deterministic subset of Python syntax, executed on this package's
own interpreter rather than by CPython. Config files are ordinary `.py` — an IDE
and `mypy` typecheck them — but evaluation is bounded and sandboxed.

Public entrypoint: `evaluate_source`, returning
`Result[ResourceRegistry, AtlantideError]`.

| Module | Purpose |
| --- | --- |
| `validate.py` | Parses with `ast` and rejects anything outside the subset: unbounded loops, exceptions, dunder access, `str.format`, and any import outside the config surface (`atlantide.core`, `.policy`, `.providers.*`, `.components.*`). Classes are rejected too, with one carve-out: a module-level `class X(EnvSchema)` whose body is only annotated fields — data, so determinism is untouched, and the only way a type checker can complete `env.<var>`. |
| `interp.py` | Tree-walking evaluator. Re-checks the import allow-list where names are bound, normalises set iteration to sorted order, and meters evaluation with a fuel counter. |
| `builtins.py` | The config global namespace: safe builtins plus pure derived functions (`uuid5`, `sha256_hex`, `to_json`, `merge`, `slugify`). |

Determinism is structural: no clock, randomness, environment, network, or
filesystem is reachable from the namespace, and every construct that could
diverge or run unbounded is rejected before evaluation starts.
