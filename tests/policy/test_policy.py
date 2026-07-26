"""Policy engine: builtins, registry, enforce scoping, and engine integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from atlantide.core import PolicyLevel, SecretRef, is_successful
from atlantide.core.errors import PolicyConfigError, PolicyViolationError, RegistryError
from atlantide.engine import Engine
from atlantide.policy import (
    DENY_DESTROY,
    REQUIRE_SECRET_REFS,
    REQUIRE_TAGS,
    PolicyContext,
    PolicyRegistry,
    PolicyResult,
    default_policy_registry,
)
from atlantide.reconcile import Action
from tests.support import Bucket, FakeProvider, Thing, Vault, engine_for, globals_of

GLOBALS = globals_of(Thing)


def _engine(policies: PolicyRegistry | None = None) -> Engine:
    provider = FakeProvider(name="test", on_create={"out": "x"}, on_update={"out": "x"})
    return engine_for(Thing, provider=provider, policies=policies)


# -- builtin policies (unit) -------------------------------------------------


def _tags_check(params: dict[str, Any] | None = None) -> Callable[..., PolicyResult]:
    reg = default_policy_registry()

    def check(**tags: str) -> PolicyResult:
        resource = Thing("a", size=1, tags=tags)
        ctx = PolicyContext("id", Action.CREATE, "default", resource, params or {})
        return reg.evaluate(REQUIRE_TAGS, ctx)

    return check


def test_require_tags_demands_any_tag_when_no_keys_are_named() -> None:
    check = _tags_check()
    assert check(env="dev").passed
    assert not check().passed
    assert "has no tags" in check().message


def test_require_tags_demands_the_keys_its_binding_names() -> None:
    check = _tags_check({"keys": ["env", "owner"]})

    assert check(env="dev", owner="platform").passed
    assert check(env="dev", owner="platform", extra="fine").passed  # extras allowed
    assert not check(env="dev").passed
    assert "owner" in check(env="dev").message


def test_require_tags_reports_every_missing_key_sorted() -> None:
    result = _tags_check({"keys": ["owner", "env", "cost-centre"]})(team="x")
    assert result.message.endswith("missing tag(s): cost-centre, env, owner")


def test_require_tags_treats_a_blank_value_as_missing() -> None:
    """A key present with an empty value carries nothing."""
    assert not _tags_check({"keys": ["env"]})(env="").passed


def test_require_tags_accepts_a_single_key_name() -> None:
    """`keys="env"` must not become the keys e, n, v."""
    check = _tags_check({"keys": "env"})
    assert check(env="dev").passed
    assert not check(e="x", n="y", v="z").passed


def test_require_tags_rejects_a_malformed_keys_argument() -> None:
    with pytest.raises(PolicyConfigError, match="keys"):
        _tags_check({"keys": 5})(env="dev")


def test_require_tags_skips_resources_with_no_tags_field() -> None:
    reg = default_policy_registry()
    ctx = PolicyContext("id", Action.DELETE, "default", None, {"keys": ["env"]})
    assert reg.evaluate(REQUIRE_TAGS, ctx).passed


def _destroy_check(
    params: dict[str, Any] | None = None, name: str = DENY_DESTROY
) -> Callable[..., bool]:
    reg = default_policy_registry()

    def check(action: Action, stack: str) -> bool:
        ctx = PolicyContext("n", action, stack, None, params or {})
        return reg.evaluate(name, ctx).passed

    return check


def test_deny_destroy_guards_the_stacks_its_binding_names() -> None:
    check = _destroy_check({"stacks": ["prod", "staging"]})

    assert not check(Action.DELETE, "prod")  # destructive in a protected stack
    assert not check(Action.REPLACE, "prod")
    assert not check(Action.DELETE, "staging")
    assert check(Action.DELETE, "dev")  # unprotected stack
    assert check(Action.CREATE, "prod")  # non-destructive


def test_deny_destroy_accepts_a_single_stack_name() -> None:
    """`stacks="prod"` must not iterate into the characters p, r, o, d."""
    check = _destroy_check({"stacks": "prod"})
    assert not check(Action.DELETE, "prod")
    assert check(Action.DELETE, "p")


def test_deny_destroy_protects_nothing_without_stacks() -> None:
    """No stack name is assumed: `prod` is a convention, and guarding it unasked
    is as wrong as missing the stack the author meant."""
    check = _destroy_check()

    assert check(Action.DELETE, "prod")
    assert check(Action.DELETE, "production")


def test_deny_destroy_rejects_a_malformed_stacks_argument() -> None:
    with pytest.raises(PolicyConfigError, match="stacks"):
        _destroy_check({"stacks": 5})(Action.DELETE, "prod")


def test_deny_destroy_is_reachable_under_its_former_name() -> None:
    check = _destroy_check({"stacks": ["prod"]}, "deny-destroy-in-prod")
    assert not check(Action.DELETE, "prod")
    assert check(Action.DELETE, "dev")


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


# -- deny-destroy-in-protected through the engine ----------------------------

_GUARD = _ENFORCE + f"enforce({DENY_DESTROY!r}, stacks=['prod'])\n"


def _stacked(stack: str, size: int) -> str:
    return (
        f"{_GUARD}from atlantide.core import Stack\n"
        f"with Stack({stack!r}, region='eu-north-1'):\n"
        f"    Thing('a', size={size}, tags={{'env': {stack!r}}})\n"
    )


def _apply(engine: Engine, src: str) -> Any:
    import asyncio

    return asyncio.run(engine.apply(src, extra_globals=GLOBALS))


def test_replace_in_a_protected_stack_is_blocked() -> None:
    """`size` is immutable, so changing it is a REPLACE — a destroy plus a
    create, which the guard denies for every resource in the stack."""
    engine = _engine()
    assert is_successful(_apply(engine, _stacked("prod", 1)))

    plan = engine.plan(_stacked("prod", 2), extra_globals=GLOBALS).unwrap()
    assert [v.policy for v in plan.blocked] == [DENY_DESTROY]

    result = _apply(engine, _stacked("prod", 2))
    assert not is_successful(result)
    assert isinstance(result.failure(), PolicyViolationError)


def test_the_same_change_is_allowed_in_an_unprotected_stack() -> None:
    engine = _engine()
    assert is_successful(_apply(engine, _stacked("dev", 1)))
    assert engine.plan(_stacked("dev", 2), extra_globals=GLOBALS).unwrap().blocked == ()


def test_removing_a_resource_from_a_protected_stack_is_blocked() -> None:
    """The DELETE an apply derives from a dropped resource is destructive too."""
    engine = _engine()
    assert is_successful(_apply(engine, _stacked("prod", 1)))

    emptied = (
        f"{_GUARD}from atlantide.core import Stack\n"
        "with Stack('prod', region='eu-north-1'):\n"
        "    pass\n"
    )
    blocked = engine.plan(emptied, extra_globals=GLOBALS).unwrap().blocked
    assert [v.policy for v in blocked] == [DENY_DESTROY]


def test_destroy_is_guarded_by_prevent_destroy_not_by_policy() -> None:
    """`destroy` has no config, so it evaluates no policy bindings. The
    resource-level `prevent_destroy` is what covers that path."""
    import asyncio

    engine = _engine()
    assert is_successful(_apply(engine, _stacked("prod", 1)))

    assert is_successful(asyncio.run(engine.destroy()))
    assert engine.backend.load().nodes == {}


# -- require-secret-refs -----------------------------------------------------


def _secret_check(resource: Any) -> PolicyResult:
    reg = default_policy_registry()
    return reg.evaluate(
        REQUIRE_SECRET_REFS, PolicyContext("id", Action.CREATE, "default", resource)
    )


def test_require_secret_refs_accepts_a_handle() -> None:
    assert _secret_check(Vault("v", token=SecretRef("app/key"))).passed


def test_require_secret_refs_rejects_a_literal() -> None:
    """A literal puts the plaintext in the config, the IR, state, and the
    artifact — the three places SecretRef exists to keep it out of."""
    result = _secret_check(Bucket("b", bucket_name="n", token="hunter2"))
    assert not result.passed
    assert "token" in result.message and "SecretRef" in result.message


def test_require_secret_refs_ignores_an_unset_field() -> None:
    """An empty value is the field's default, not a secret."""
    assert _secret_check(Bucket("b", bucket_name="n")).passed
    assert _secret_check(Vault("v")).passed  # SecretRef | None, defaults to None


def test_require_secret_refs_ignores_computed_outputs() -> None:
    """A generated secret is produced by the provider; it is sensitive but never
    a literal in config."""
    from atlantide.providers.random import Password

    assert _secret_check(Password("p", length=16)).passed


def test_require_secret_refs_skips_a_pure_delete() -> None:
    assert _secret_check(None).passed


def test_a_secret_ref_typed_field_rejects_a_literal_before_the_policy_runs() -> None:
    """`secret()` declares the field `SecretRef | None`, so pydantic refuses a
    literal at construction. The policy covers the other case: a `sensitive`
    field typed as a plain `str`."""
    with pytest.raises(Exception, match="SecretRef"):
        Vault("v", token="hunter2")


def test_require_secret_refs_blocks_an_apply() -> None:
    engine = engine_for(Bucket, provider=FakeProvider(name="test", on_create={"arn": "a"}))
    src = (
        _ENFORCE
        + f"enforce({REQUIRE_SECRET_REFS!r})\n"
        + "Bucket('b', bucket_name='n', token='hunter2')\n"
    )
    plan = engine.plan(src, extra_globals=globals_of(Bucket)).unwrap()
    assert [v.policy for v in plan.blocked] == [REQUIRE_SECRET_REFS]


def test_enforce_types_accepts_a_bare_string() -> None:
    engine = _engine()
    # A lone type name must scope to that type; `frozenset(str)` would silently
    # become a set of characters and the policy would never match anything.
    src = _ENFORCE + f"enforce('require-tags', types='{Thing.type_name()}')\nThing('a', size=1)\n"
    plan = engine.plan(src, extra_globals=GLOBALS).unwrap()
    assert len(plan.blocked) == 1


def test_enforce_types_empty_is_refused() -> None:
    engine = _engine()
    src = _ENFORCE + "enforce('require-tags', types=[])\nThing('a', size=1)\n"
    result = engine.plan(src, extra_globals=GLOBALS)
    assert not is_successful(result)
    assert isinstance(result.failure(), PolicyConfigError)
