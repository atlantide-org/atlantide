# atlantide.policy

Per-resource policy: rules that inspect one node's pending change and either
warn or block the apply.

Policies are evaluated at plan time. A `mandatory` violation blocks apply; an
`advisory` one is reported as a warning.

| Module | Purpose |
| --- | --- |
| `base.py` | The interfaces: `PolicyContext` (the node and its pending change), `PolicyFn`, `PolicyResult`, `Violation`, and the `PolicyProvider` a rule set implements. |
| `binding.py` | How policies attach to resources: `enforce(...)` at config level, `@policy` at class level. |
| `builtin.py` | The native-Python provider that ships with atlantide, plus the default registry. |
| `registry.py` | Resolves a policy name to the provider that defines it. |

Bindings are recorded in the `.atlas` artifact, so a deploy evaluates the same
policy set the build did.

## Builtins

- `require-tags` — every taggable resource carries the tag keys the binding
  names, or any tag at all when it names none:

  ```python
  enforce("require-tags", keys=["env", "owner"])
  ```

  A key present with an empty value counts as missing. Resources without a
  `tags` field are skipped.
- `require-secret-refs` — no `sensitive` field holds a literal:

  ```python
  enforce("require-secret-refs")
  ```

  Takes no arguments. A field declared with `secret()` is typed
  `SecretRef | None`, so pydantic already refuses a literal there; this covers
  the case the annotation does not — `mutable(..., sensitive=True)` on a plain
  `str`, which accepts plaintext silently. Computed and empty values are
  skipped.
- `deny-destroy-in-protected` — no DELETE or REPLACE in a protected stack.
  Which stacks those are is an argument to the binding:

  ```python
  enforce("deny-destroy-in-protected", stacks=["prod"])
  ```

  With no `stacks` the policy is inert, since a stack named `prod` is a
  convention rather than something atlantide can infer. Reachable under its
  former name `deny-destroy-in-prod`.

## Parameterised policies

Keywords passed to `enforce` beyond `level` and `types` become the binding's
`params`, and reach the policy as `PolicyContext.params`. They are plain
deterministic data from Atlas-lang and round-trip through the `.atlas` artifact,
so a `deploy` evaluates each policy with the arguments the build gave it. A
policy validates its own arguments and raises `PolicyConfigError` on a bad one.
