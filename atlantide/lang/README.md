# atlantide.lang

Atlas-lang: a deterministic subset of Python syntax, executed on this package's
own interpreter rather than by CPython. Config files are ordinary `.py` — an IDE
and `mypy` typecheck them — but evaluation is bounded and sandboxed.

Public entrypoint: `evaluate_source`, returning
`Result[ResourceRegistry, AtlantideError]`.

| Module | Purpose |
| --- | --- |
| `validate/` | Parses with `ast` and rejects anything outside the subset: unbounded loops, exceptions, dunder access, `str.format`, and any import outside the config surface (`atlantide.core`, `.policy`, `.providers.*`, `.components.*`). Classes are rejected too, with one carve-out: a module-level `class X(EnvSchema)` whose body is only annotated fields — data, so determinism is untouched, and the only way a type checker can complete `env.<var>`. `imports.py` holds the import allow-list — the sandbox policy in one file; `rules.py` the node/name/attribute rules; `schema.py` the `EnvSchema` carve-out; `validator.py` the AST pass. The subset is an allow-list of node types (`rules.ALLOWED_NODES`); `ClassDef` is deliberately absent from it and checked by `validator._Validator.visit_ClassDef` instead, because a class is permitted in one exact shape only. The two checks a syntactic pass cannot make — that the base really is `EnvSchema`, and that a default never lands in the class namespace — are made by `interp.statements.StatementsMixin._st_ClassDef`. |
| `interp/` | Tree-walking evaluator. Re-checks the import allow-list where names are bound (`binding.py`), normalises set iteration to sorted order, renders every value as text through one deterministic renderer — sets at any depth in that same order, text embedding a memory address refused (`stability.py`), and meters evaluation with a fuel counter, pricing native work up front (`costs.py`). `evaluator.py` holds the counter and the by-name dispatch to the `_st_*`/`_ex_*` handlers in `statements.py` and `expressions.py`; `interpreter.py` combines them into `Interpreter`. `scope.py` holds `Scope`, `Closure` and the control-flow signals; `operators.py` the AST operator tables. |
| `surface.py` | Static audit of the containers the allowed modules export, applying the interpreter's bind-time check to every element. |
| `builtins.py` | The config global namespace: safe builtins plus pure derived functions (`uuid5`, `sha256_hex`, `to_json`, `merge`, `slugify`). |

Determinism is structural: no clock, randomness, environment, network, or
filesystem is reachable from the namespace, and every construct that could
diverge or run unbounded is rejected before evaluation starts.
