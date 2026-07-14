"""Policy engine: builtins, registry, enforce scoping, and engine integration."""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.core import PolicyLevel, is_successful
from atlantide.core.errors import PolicyViolationError, RegistryError
from atlantide.engine import Engine
from atlantide.policy import PolicyContext, PolicyRegistry, default_policy_registry
from atlantide.reconcile import Action
from tests.support import FakeProvider, Thing, engine_for, globals_of

GLOBALS = globals_of(Thing)


def _engine(policies: PolicyRegistry | None = None) -> Engine:
    provider = FakeProvider(name="test", on_create={"out": "x"}, on_update={"out": "x"})
    return engine_for(Thing, provider=provider, policies=policies)


# -- builtin policies (unit) -------------------------------------------------


def test_require_tags_builtin() -> None:
    reg = default_policy_registry()
    tagged = PolicyContext("id", Action.CREATE, "default", Thing("a", size=1, tags={"e": "x"}))
    untagged = PolicyContext("id", Action.CREATE, "default", Thing("b", size=1))
    assert reg.evaluate("require-tags", tagged).passed
    assert not reg.evaluate("require-tags", untagged).passed


def test_deny_destroy_in_prod_builtin() -> None:
    reg = default_policy_registry()

    def check(action: Action, stack: str) -> bool:
        return reg.evaluate("deny-destroy-in-prod", PolicyContext("n", action, stack, None)).passed

    assert not check(Action.DELETE, "prod")  # destructive in prod -> blocked
    assert not check(Action.REPLACE, "prod")
    assert check(Action.DELETE, "dev")  # non-prod ok
    assert check(Action.CREATE, "prod")  # non-destructive ok


def test_unknown_policy_raises() -> None:
    with pytest.raises(RegistryError, match="unknown policy"):
        default_policy_registry().evaluate("nope", PolicyContext("i", Action.CREATE, "d", None))


# -- enforce + engine integration --------------------------------------------

_ENFORCE = "from atlantide.policy import enforce\n"


def test_enforce_global_advisory_warns_but_allows() -> None:
    engine = _engine()
    src = _ENFORCE + "enforce('require-tags', level=PolicyLevel.ADVISORY)\nThing('a', size=1)\n"
    plan = engine.plan(src, extra_globals={**GLOBALS, "PolicyLevel": PolicyLevel}).unwrap()
    assert len(plan.violations) == 1
    assert plan.violations[0].level is PolicyLevel.ADVISORY
    assert plan.blocked == ()  # advisory does not block


def test_enforce_mandatory_blocks_apply() -> None:
    engine = _engine()
    src = _ENFORCE + "enforce('require-tags')\nThing('a', size=1)\n"  # untagged -> mandatory fail
    plan = engine.plan(src, extra_globals=GLOBALS).unwrap()
    assert len(plan.blocked) == 1

    result = _run_apply(engine, src)
    assert not is_successful(result)
    assert isinstance(result.failure(), PolicyViolationError)


def test_enforce_passes_when_satisfied() -> None:
    engine = _engine()
    src = _ENFORCE + "enforce('require-tags')\nThing('a', size=1, tags={'env': 'dev'})\n"
    plan = engine.plan(src, extra_globals=GLOBALS).unwrap()
    assert plan.violations == ()


def test_enforce_type_scoped_only_matches_type() -> None:
    engine = _engine()
    # scope to a different type -> no violation even though Thing is untagged
    src = _ENFORCE + "enforce('require-tags', types=['aws.S3Bucket'])\nThing('a', size=1)\n"
    plan = engine.plan(src, extra_globals=GLOBALS).unwrap()
    assert plan.violations == ()


def test_noop_nodes_are_not_policy_checked() -> None:
    engine = _engine()
    src = _ENFORCE + "enforce('require-tags')\nThing('a', size=1, tags={'e': 'x'})\n"
    import asyncio

    asyncio.run(engine.apply(src, extra_globals=GLOBALS))  # create, tagged -> ok
    # second plan: node is NOOP -> policy not evaluated, no violations
    plan = engine.plan(src, extra_globals=GLOBALS).unwrap()
    assert plan.violations == ()


def _run_apply(engine: Engine, src: str) -> Any:
    import asyncio

    return asyncio.run(engine.apply(src, extra_globals=GLOBALS))
