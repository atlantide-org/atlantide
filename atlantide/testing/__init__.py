"""atlantide.testing: compile a component and plan runs over it, as the engine does.

The public test API for component authors; see ``README.md``. Not importable from
config.

    first = Compiled.of(lambda: MyComponent("site"), region="eu-north-1")
    assert set(first.against().actions.values()) == {Action.CREATE}
    assert set(first.against(first).actions.values()) == {Action.NOOP}
"""

from atlantide.reconcile import Action, Change, ChangeSet
from atlantide.testing.compiled import Compiled, Plan, local_names, stack

__all__ = [
    "Action",
    "Change",
    "ChangeSet",
    "Compiled",
    "Plan",
    "local_names",
    "stack",
]
