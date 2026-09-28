"""Regression tests: policy level/types validation and ``@policy`` in the planner."""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.core import PolicyLevel, is_successful
from atlantide.core.errors import PolicyConfigError
from atlantide.policy import REQUIRE_TAGS, policy
from tests.support import FakeProvider, Thing, engine_for, globals_of

GLOBALS = globals_of(Thing)
_ENFORCE = "from atlantide.policy import enforce\n"


def _plan(src: str, *classes: type[Thing]) -> Any:
    classes = classes or (Thing,)
    provider = FakeProvider(name="test", on_create={"out": "x"})
    engine = engine_for(*classes, provider=provider)
    return engine.plan(src, extra_globals=globals_of(*classes))


# -- level ---------------------------------------------------------------------


def test_enforce_accepts_the_level_as_its_string_value_and_blocks() -> None:
    """`level="mandatory"` must be the enum, or the planner's identity check
    never blocks on it."""
    src = _ENFORCE + "enforce('require-tags', level='mandatory')\nThing('a', size=1)\n"
    plan = _plan(src).unwrap()
    assert len(plan.blocked) == 1
    assert plan.blocked[0].level is PolicyLevel.MANDATORY


def test_enforce_accepts_advisory_as_a_string() -> None:
    src = _ENFORCE + "enforce('require-tags', level='advisory')\nThing('a', size=1)\n"
    plan = _plan(src).unwrap()
    assert [v.level for v in plan.violations] == [PolicyLevel.ADVISORY]
    assert plan.blocked == ()


@pytest.mark.parametrize("level", ["'mandatroy'", "'MANDATORY'", "1", "None"])
def test_enforce_rejects_an_unknown_level(level: str) -> None:
    src = _ENFORCE + f"enforce('require-tags', level={level})\nThing('a', size=1)\n"
    result = _plan(src)
    assert not is_successful(result)
    error = result.failure()
    assert isinstance(error, PolicyConfigError)
    assert "`level` must be one of 'advisory', 'mandatory'" in str(error)


def test_policy_decorator_coerces_a_string_level() -> None:
    @policy(REQUIRE_TAGS, level="advisory")  # type: ignore[arg-type]
    class Loose(Thing):
        pass

    from atlantide.policy.binding import class_bindings

    (binding,) = class_bindings(Loose)
    assert binding.level is PolicyLevel.ADVISORY


def test_policy_decorator_rejects_an_unknown_level() -> None:
    with pytest.raises(PolicyConfigError, match="`level` must be one of"):
        policy(REQUIRE_TAGS, level="blocking")  # type: ignore[arg-type]


# -- types ---------------------------------------------------------------------


@pytest.mark.parametrize("types", ["[Thing]", "['test.Thing', 3]"])
def test_enforce_rejects_a_non_string_type_name(types: str) -> None:
    """A class would silently match nothing; mixed items would crash sorting."""
    src = _ENFORCE + f"enforce('require-tags', types={types})\nThing('a', size=1)\n"
    result = _plan(src)
    assert not is_successful(result)
    error = result.failure()
    assert isinstance(error, PolicyConfigError)
    assert "`types` must name resource types as strings" in str(error)


# -- @policy through the planner (class_bindings path) -------------------------


@policy(REQUIRE_TAGS)
class TaggedThing(Thing):
    """A resource whose class carries a mandatory require-tags binding."""


class SubTaggedThing(TaggedThing):
    """Undecorated subclass of a decorated resource."""


def test_decorated_resource_passes_when_satisfied() -> None:
    src = "TaggedThing('a', size=1, tags={'env': 'dev'})\n"
    plan = _plan(src, TaggedThing).unwrap()
    assert plan.violations == ()


def test_decorated_resource_is_blocked_when_violated() -> None:
    plan = _plan("TaggedThing('a', size=1)\n", TaggedThing).unwrap()
    assert [(v.policy, v.level) for v in plan.blocked] == [(REQUIRE_TAGS, PolicyLevel.MANDATORY)]


def test_undecorated_resource_is_not_checked() -> None:
    plan = _plan("Thing('a', size=1)\n").unwrap()
    assert plan.violations == ()


def test_subclass_inherits_the_decorated_binding() -> None:
    """Current behaviour: `class_bindings` reads an inherited class attribute and
    the planner applies it without re-checking the binding's type set."""
    plan = _plan("SubTaggedThing('a', size=1)\n", SubTaggedThing).unwrap()
    assert [v.policy for v in plan.blocked] == [REQUIRE_TAGS]
    ok = _plan("SubTaggedThing('a', size=1, tags={'e': 'x'})\n", SubTaggedThing)
    assert ok.unwrap().violations == ()
