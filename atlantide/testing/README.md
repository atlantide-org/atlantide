# atlantide.testing

The public test API for component authors: compile a component, then plan runs
over it exactly as `atlantide plan` would, without applying anything. A component
test imports only `atlantide.testing`, plus `atlantide.core` and the providers it
builds with.

Config cannot import it: it sits outside the import allow-list
(`lang/validate/imports.py`), and no other atlantide package imports it
(`lint-imports`).

| Name | What it is |
| --- | --- |
| `Compiled.of(build, *, stack="infra", region, name_prefix="acme", tags=None, types=None)` | Calls `build()` inside a `Stack`, then lowers and hashes what it declared. |
| `Compiled.empty()` | A config that declares nothing. Planned over a prior config, it gives a teardown. |
| `compiled.ir`, `.hashes`, `.bytes`, `.names` | The IR graph, each node's Merkle hash, the canonical IR bytes, and the local names. |
| `compiled["site-page"]` | The `IRNode` with that local name. Raises `KeyError` when none or several nodes match. |
| `compiled.state()` | The `StateGraph` an apply of this config would have committed. |
| `compiled.against(prior=None)` | The `Plan` for a run of this config over `prior`'s committed state, or over empty state when `prior` is omitted. `prior` is a `Compiled`, a `Plan` (the state applying it commits) or a `StateGraph`. |
| `plan.changes`, `.actions`, `plan["site-page"]` | The `ChangeSet`, the `Action` for each local name, and the `Change` for one node (`changed_fields`, `conditional`, `state_only`). |
| `plan.state_only` | Local names whose apply only rewrites the state row: a `prevent_destroy` change on an otherwise unchanged node (a NOOP, no provider call). |
| `plan.protected`, `plan.approve()` | The ids the `prevent_destroy` guard protects (the config's flag for a node it declares, the committed flag for one it drops), and the planner's verdict: `Failure(PreventDestroyError)` when the plan deletes or replaces one of them. A protected conditional REPLACE behind upstreams that keep their identity is approved; the apply judges it. |
| `plan.committed()` | The `StateGraph` an apply of the plan would commit: prior rows for NOOPs (with the new flag for a state-only change), the planned rows for created, updated and replaced nodes, deleted rows dropped. |
| `stack(name="infra", *, region, name_prefix="acme", tags=None)` | A `Stack` to build in, for assertions on the resource objects. |
| `local_names(*children, name)` | The local names of a component's children: `local_names("page", name="site") == {"site-page"}`. |
| `Action`, `Change`, `ChangeSet` | Re-exported from `atlantide.reconcile`. |

## Same code paths as a run

- **Lowering and hashing** go through `engine.compiler.compile_registry`, the
  function `Engine.compile` uses: it inlines stack outputs, lowers to IR, builds
  the graph and computes Merkle hashes in topological order. No provider is built,
  so `provider_version` is empty in the IR. The version is not part of any hash.
- **Diffing** calls `reconcile.diff`. `approve()` calls
  `reconcile.check_prevent_destroy` over `engine.planner.protected_ids(state, ir)`.
  These are the calls the planner makes before its secret and policy passes.
- **What is not modelled** is the apply-time confirmation of a conditional
  REPLACE: nothing is applied, so no upstream output is known. `conditional` says
  the replace may collapse into an update; `committed()` writes the same row
  either way.
- **Mutability** is built by `reconcile.type_mutability`, the function the
  `Engine` uses. It covers every type the installed provider plugins declare,
  found by `providers.loader.discover()` (so `ATLANTIDE_NO_PLUGINS` applies) and
  cached for the process. A plugin refused over its identity raises
  `RegistryError`, just as it aborts a run. `types=` replaces the discovered set,
  for example with a provider under development that is not installed. A node
  whose type has no mutability entry makes `Compiled.of` raise, because without
  one every changed field would be classified as mutable.
- **`state()`** writes each row the way the executor does, from the IR node and its
  hash, with status `created`. Provider outputs and secret digests are left
  empty; the diff does not read them.

## Example

The pipeline tests below each compile the component and assert the action a run
would take.

```python
from returns.pipeline import is_successful

from atlantide.core import PreventDestroyError
from atlantide.testing import Action, Compiled, local_names

from src import Site  # the component under test

CORE = local_names("page", "token", name="site")


def compiled(**kwargs) -> Compiled:
    return Compiled.of(lambda: Site("site", **kwargs), region="eu-north-1")


def test_first_run_creates_every_node() -> None:
    run = compiled().against()
    assert set(run.actions) == CORE
    assert set(run.actions.values()) == {Action.CREATE}


def test_unchanged_config_is_all_noop() -> None:
    assert compiled().bytes == compiled().bytes
    assert set(compiled().against(compiled()).actions.values()) == {Action.NOOP}


def test_mutable_change_updates_immutable_change_replaces() -> None:
    update = compiled(content="bye").against(compiled())
    assert update.actions["site-page"] is Action.UPDATE
    assert update["site-page"].changed_fields == ("content",)

    replace = compiled(path="about.html").against(compiled())
    assert replace.actions["site-page"] is Action.REPLACE


def test_an_immutable_ref_replaces_conditionally() -> None:
    # `token.keepers` holds a Ref to the page, resolved only at apply.
    token = compiled(content="bye").against(compiled())["site-token"]
    assert token.action is Action.REPLACE
    assert token.conditional


def test_teardown_is_refused_while_protected() -> None:
    run = Compiled.empty().against(compiled(protect=True))
    assert set(run.actions.values()) == {Action.DELETE}
    assert isinstance(run.approve().failure(), PreventDestroyError)
    assert is_successful(Compiled.empty().against(compiled()).approve())


def test_protecting_existing_infra_takes_effect_at_once() -> None:
    run = compiled(protect=True).against(compiled())  # a protect-only edit
    assert run.state_only == {"site-page"}  # recorded by the apply, no provider call
    teardown = Compiled.empty().against(run)  # over the state that apply commits
    assert isinstance(teardown.approve().failure(), PreventDestroyError)
```

`tests/testing/` runs these cases against a small `local`/`random` component.
