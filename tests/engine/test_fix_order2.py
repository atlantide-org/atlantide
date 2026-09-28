"""Create-before-destroy propagates to what a create-before-destroy node depends on.

``x`` is create-before-destroy and consumes ``y.out``. Replacing ``y``
destroy-first would delete it while ``x`` (kept alive by its own guarantee) still
uses it. Terraform's rule, and this one: ``y`` is create-before-destroy too, so
its ``~replaced`` companion row must be inside the apply's lock scope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from atlantide.core import AtlantideError, Resource, SecretRef, computed, immutable
from atlantide.core.fields import field_mutability
from atlantide.core.lifecycle import Lifecycle
from atlantide.engine.locking import apply_scope
from atlantide.engine.secret_audit import SecretAudit
from atlantide.ir.model import IRNode
from atlantide.reconcile import Action, Change, ChangeSet
from atlantide.secrets import KeyMaterial, SecretsRegistry
from atlantide.secrets.env import EnvSecretsProvider
from atlantide.state import MemoryStateBackend, StateBackend
from atlantide.state.sql.sqlite import SqliteStateBackend
from tests.support import (
    Box,
    Bucket,
    FakeProvider,
    Notifier,
    Server,
    engine_for,
    globals_of,
    state_node,
)

GLOBALS = globals_of(Box, Notifier, Lifecycle=Lifecycle)
X = "default:test.Notifier:x"
Y = "default:test.Box:y"


def _src(size: int, *, depends_on: bool = False) -> str:
    """``x`` (CBD) consumes ``y.out`` through an immutable field, or only orders after ``y``."""
    target = "'fixed', depends_on=[y]" if depends_on else "y.out"
    return (
        f"y = Box('y', size={size})\n"
        f"Notifier('x', target_arn={target}, "
        "lifecycle=Lifecycle(create_before_destroy=True))\n"
    )


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> StateBackend:
    if request.param == "memory":
        return MemoryStateBackend()
    return SqliteStateBackend(str(tmp_path / "s.db"))


async def test_an_upstream_of_a_cbd_node_is_replaced_create_first(backend: StateBackend) -> None:
    provider = FakeProvider()
    engine = engine_for(Box, Notifier, provider=provider, backend=backend)
    (await engine.apply(_src(1), extra_globals=GLOBALS)).unwrap()

    plan = engine.plan(_src(2), extra_globals=GLOBALS).unwrap()
    changes = {c.node_id: c for c in plan.changeset}
    assert (changes[Y].action, changes[Y].create_before_destroy) == (Action.REPLACE, True)
    assert f"{Y}~replaced" in apply_scope(plan, backend.load())

    provider.reset()
    report = (await engine.apply(_src(2), extra_globals=GLOBALS)).unwrap()

    # The old y outlives the old x: nothing is deleted while x still uses it.
    assert provider.calls == [
        ("create", "y"),
        ("create", "x"),
        ("delete", "x"),
        ("delete", "y"),
    ]
    assert sorted(report.replaced) == [Y, X]
    assert report.downgraded == {}
    assert set(backend.load().nodes) == {X, Y}


async def test_an_ordering_edge_propagates_too(backend: StateBackend) -> None:
    """``depends_on`` orders ``x`` after ``y`` as a ref does; ``x`` is only re-applied."""
    provider = FakeProvider()
    engine = engine_for(Box, Notifier, provider=provider, backend=backend)
    (await engine.apply(_src(1, depends_on=True), extra_globals=GLOBALS)).unwrap()
    provider.reset()

    report = (await engine.apply(_src(2, depends_on=True), extra_globals=GLOBALS)).unwrap()

    assert provider.calls == [("create", "y"), ("update", "x"), ("delete", "y")]
    assert report.replaced == [Y]
    assert set(backend.load().nodes) == {X, Y}


# -- identity collisions (the planner's create-before-destroy resolution) ----------

SERVER_GLOBALS = globals_of(Server, Notifier, Bucket, Lifecycle=Lifecycle)


def _served(zone: str, *, declared: bool = False) -> str:
    """``y`` keeps its physical name, so it cannot be replaced create-first."""
    own = ", lifecycle=Lifecycle(create_before_destroy=True)" if declared else ""
    return (
        f"y = Server('y', name='web', zone={zone!r}{own})\n"
        "Notifier('x', target_arn='t', depends_on=[y], "
        "lifecycle=Lifecycle(create_before_destroy=True))\n"
    )


@pytest.mark.parametrize("declared", [False, True])
async def test_a_colliding_upstream_of_a_cbd_node_fails_the_plan(declared: bool) -> None:
    provider = FakeProvider()
    engine = engine_for(Server, Notifier, provider=provider, backend=MemoryStateBackend())
    (await engine.apply(_served("a", declared=declared), extra_globals=SERVER_GLOBALS)).unwrap()
    provider.reset()

    planned = engine.plan(_served("b", declared=declared), extra_globals=SERVER_GLOBALS)

    error = planned.failure()
    assert isinstance(error, AtlantideError)
    assert "default:test.Server:y" in str(error) and X in str(error)
    applied = await engine.apply(_served("b", declared=declared), extra_globals=SERVER_GLOBALS)
    assert isinstance(applied.failure(), AtlantideError)
    assert provider.calls == []


def _bucket_arn(_: Any, res: Any) -> dict[str, Any]:
    return {"arn": f"arn:{res.bucket_name}:{res.region}"} if isinstance(res, Bucket) else {}


async def test_a_downgraded_upstream_takes_its_conditional_dependent_down_first() -> None:
    """``b`` declares create-before-destroy but keeps its name: the planner makes it
    destroy-first, so ``n`` (conditional on ``b.arn``) must be deleted before it."""
    provider = FakeProvider(on_create=_bucket_arn)
    engine = engine_for(Bucket, Notifier, provider=provider, backend=MemoryStateBackend())

    def src(region: str) -> str:
        return (
            f"b = Bucket('b', bucket_name='b', region={region!r}, "
            "lifecycle=Lifecycle(create_before_destroy=True))\n"
            "Notifier('n', target_arn=b.arn)\n"
        )

    (await engine.apply(src("r1"), extra_globals=SERVER_GLOBALS)).unwrap()
    plan = engine.plan(src("r2"), extra_globals=SERVER_GLOBALS).unwrap()
    changes = {c.node_id: c for c in plan.changeset}
    assert changes["default:test.Bucket:b"].create_before_destroy is False
    assert plan.warnings  # the downgrade is still reported
    n = changes["default:test.Notifier:n"]
    assert (n.action, n.conditional) == (Action.REPLACE, False)
    provider.reset()

    report = (await engine.apply(src("r2"), extra_globals=SERVER_GLOBALS)).unwrap()

    assert provider.calls == [("delete", "n"), ("delete", "b"), ("create", "b"), ("create", "n")]
    assert report.downgraded == {}


# -- a rotated immutable secret ------------------------------------------------------


class Locked(Resource):
    """A resource whose sensitive field is also immutable."""

    class Meta:
        provider: ClassVar[str] = "test"

    key: str = immutable(sensitive=True)
    out: str = computed()


def _locked_out(_: Any, res: Any) -> dict[str, Any]:
    return {"out": f"{res.logical_name}:out"} if isinstance(res, Locked) else {}


async def test_a_rotation_replace_of_a_cbd_upstream_is_create_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = SecretsRegistry(material=KeyMaterial(str(tmp_path / "k.key")))
    secrets.register(EnvSecretsProvider(allow=["S*"]), default=True)
    engine = engine_for(
        Locked,
        Notifier,
        provider=FakeProvider(on_create=_locked_out),
        backend=MemoryStateBackend(),
        secrets=secrets,
    )
    source = (
        "l = Locked('l', key=SecretRef('S0'))\n"
        "Notifier('x', target_arn=l.out, lifecycle=Lifecycle(create_before_destroy=True))\n"
    )
    extra = globals_of(Locked, Notifier, SecretRef=SecretRef, Lifecycle=Lifecycle)
    monkeypatch.setenv("S0", "v1")
    (await engine.apply(source, extra_globals=extra)).unwrap()
    monkeypatch.setenv("S0", "v2")

    plan = engine.plan(source, extra_globals=extra).unwrap()

    rotated = {c.node_id: c for c in plan.changeset}["default:test.Locked:l"]
    assert (rotated.action, rotated.create_before_destroy) == (Action.REPLACE, True)


def test_the_rotation_pass_settles_conditional_dependents() -> None:
    """A rotated destroy-first REPLACE gets the diff's ordering for a conditional
    dependent with an immutable ref to it."""
    up = IRNode("u", "test.Locked", "test", "1.0.0", {"key": "k"}, ())
    down = IRNode("d", "test.Notifier", "test", "1.0.0", {"target_arn": {"$ref": "u#out"}}, ("u",))
    prior = state_node("d", type="test.Notifier")
    changeset = ChangeSet(
        (
            Change("u", Action.NOOP, desired=up, prior=state_node("u", type="test.Locked")),
            Change(
                "d",
                Action.REPLACE,
                desired=down,
                prior=prior,
                changed_fields=("target_arn",),
                conditional=True,
            ),
        )
    )
    audit = SecretAudit(rotated={"u": ("key",)}, matched=0, mismatched=frozenset())
    mutability = {
        "test.Locked": field_mutability(Locked),
        "test.Notifier": field_mutability(Notifier),
    }

    settled = {c.node_id: c for c in audit.applied_to(changeset, mutability, frozenset())}

    assert (settled["u"].action, settled["u"].create_before_destroy) == (Action.REPLACE, False)
    assert settled["d"].conditional is False
    cbd = {c.node_id: c for c in audit.applied_to(changeset, mutability, frozenset({"u"}))}
    assert cbd["u"].create_before_destroy is True
    assert cbd["d"].conditional is True  # behind a create-first upstream it may still collapse
