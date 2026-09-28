"""``resolve_cbd``: a create-before-destroy REPLACE that would collide on identity.

Pure over the changeset, the resource types and their mutability: a replacement
that keeps the old identity falls back to destroy-before-create with a warning,
unless a create-before-destroy dependent forces it, which fails the plan.
"""

from __future__ import annotations

from typing import Any

from atlantide.core.resource import Resource
from atlantide.ir.model import IRNode
from atlantide.reconcile import Action, Change, ChangeSet, type_mutability
from atlantide.reconcile.ordering import resolve_cbd
from atlantide.state import StateNode
from tests.support import Box, Server

TYPES: dict[str, type[Resource]] = {Server.type_name(): Server, Box.type_name(): Box}
MUT = type_mutability(TYPES)
S = "default:test.Server:s"
B = "default:test.Box:b"


def _replace(
    node_id: str,
    cls: type[Resource],
    desired: dict[str, Any],
    prior: dict[str, Any],
    changed: tuple[str, ...],
    *,
    cbd: bool = True,
    action: Action = Action.REPLACE,
) -> Change:
    common: dict[str, Any] = {
        "id": node_id,
        "type": cls.type_name(),
        "provider": "test",
        "provider_version": "1.0.0",
    }
    return Change(
        node_id=node_id,
        action=action,
        desired=IRNode(**common, properties=desired, dependencies=(), create_before_destroy=cbd),
        prior=StateNode(**common, input_hash="h", properties=prior),
        changed_fields=changed,
        create_before_destroy=cbd,
    )


def _server(name_desired: str, name_prior: str, **kw: Any) -> Change:
    return _replace(
        S,
        Server,
        {"name": name_desired, "zone": "b"},
        {"name": name_prior, "zone": "a"},
        ("zone",),
        **kw,
    )


def _resolve(*changes: Change, forcers: dict[str, frozenset[str]] | None = None) -> Any:
    return resolve_cbd(ChangeSet(changes), types=TYPES, mutability=MUT, forcers=forcers)


def test_same_physical_name_downgrades_to_destroy_first_with_a_warning() -> None:
    resolved, warnings = _resolve(_server("web", "web")).unwrap()
    assert resolved.changes[0].create_before_destroy is False
    assert warnings == (
        f"{S}: create_before_destroy not possible "
        "(replacement shares the old identity); using destroy-before-create",
    )


def test_a_new_physical_name_keeps_create_first() -> None:
    change = _server("web2", "web")
    resolved, warnings = _resolve(change).unwrap()
    assert resolved.changes == (change,)
    assert warnings == ()


def test_without_a_declared_identity_an_immutable_change_keeps_create_first() -> None:
    change = _replace(B, Box, {"size": 2}, {"size": 1}, ("size",))
    resolved, warnings = _resolve(change).unwrap()
    assert resolved.changes == (change,)
    assert warnings == ()


def test_without_a_declared_identity_a_mutable_only_change_collides() -> None:
    change = _replace(B, Box, {"size": 1, "label": "y"}, {"size": 1, "label": "x"}, ("label",))
    resolved, warnings = _resolve(change).unwrap()
    assert resolved.changes[0].create_before_destroy is False
    assert len(warnings) == 1


def test_an_unknown_type_is_judged_by_mutability_alone() -> None:
    change = _replace(B, Box, {"size": 1}, {"size": 1}, ("size",))
    resolved, _ = resolve_cbd(ChangeSet((change,)), types={}, mutability=MUT, forcers=None).unwrap()
    assert resolved.changes == (change,)  # size is immutable in MUT


def test_non_cbd_and_non_replace_changes_pass_through() -> None:
    dbc = _server("web", "web", cbd=False)
    update = _server("web", "web", action=Action.UPDATE)
    resolved, warnings = _resolve(dbc, update).unwrap()
    assert resolved.changes == (dbc, update)
    assert warnings == ()


def test_a_create_first_dependent_forbids_the_downgrade() -> None:
    forcers = {S: frozenset({S, "default:test.Link:z", "default:test.Link:y"})}
    error = _resolve(_server("web", "web"), forcers=forcers).failure()
    assert str(error) == (
        f"cannot replace create-before-destroy: {S} (depended on by create_before_destroy "
        "default:test.Link:y, default:test.Link:z) — the replacement shares the old "
        "identity, so it cannot be created first, and destroying it first would delete "
        "what a create_before_destroy dependent still uses. Give it a new physical name, "
        "or drop create_before_destroy from the dependent"
    )


def test_its_own_declaration_alone_does_not_force() -> None:
    resolved, warnings = _resolve(_server("web", "web"), forcers={S: frozenset({S})}).unwrap()
    assert resolved.changes[0].create_before_destroy is False
    assert len(warnings) == 1
