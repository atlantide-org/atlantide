"""Regression tests: ``Plan.actions`` ambiguity and ``Compiled`` mutability tables."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from atlantide.providers.local import Null
from atlantide.testing import Action, Compiled
from atlantide.testing import compiled as module
from tests.testing.site import NAME, REGION, Site


@pytest.fixture
def fresh_discovery() -> Iterator[None]:
    module._installed_mutability.cache_clear()
    yield
    module._installed_mutability.cache_clear()


def test_actions_refuses_a_local_name_shared_by_two_stacks() -> None:
    """Keyed by local name, one stack's action would silently overwrite the other's."""

    def build() -> None:
        with module.stack("blue", region=REGION):
            Null("twin")
        with module.stack("green", region=REGION):
            Null("twin")

    plan = Compiled.of(build, region=REGION).against()
    with pytest.raises(KeyError, match="several nodes are named 'twin'"):
        _ = plan.actions
    with pytest.raises(KeyError, match="several nodes are named 'twin'"):
        plan["twin"]


@pytest.mark.usefixtures("fresh_discovery")
def test_empty_does_not_discover_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> None:
        raise AssertionError("Compiled.empty() must not discover plugins")

    monkeypatch.setattr(module, "discover", refuse)
    assert Compiled.empty().mutability == {}


def test_empty_still_plans_a_teardown() -> None:
    prior = Compiled.of(lambda: Site(NAME), region=REGION)
    assert set(Compiled.empty().against(prior).actions.values()) == {Action.DELETE}


def test_instances_do_not_share_one_mutability_table() -> None:
    first = Compiled.of(lambda: Site(NAME), region=REGION)
    second = Compiled.of(lambda: Site(NAME), region=REGION)
    assert first.mutability == second.mutability
    assert first.mutability is not second.mutability
    assert first.mutability is not module._installed_mutability()
    type_name = next(iter(first.mutability))
    assert first.mutability[type_name] is not second.mutability[type_name]


def test_simulated_state_keeps_ordering_only_edges() -> None:
    """`Compiled.state` and `Plan.committed` write `depends_on` as apply does."""

    def build() -> None:
        first = Null("first")
        Null("second", depends_on=[first])

    config = Compiled.of(build, region=REGION)
    (second,) = (node for node in config.ir.nodes if node.id.endswith(":second"))
    assert second.depends_on  # the IR carries the edge
    for state in (config.state(), config.against().committed()):
        assert state.nodes[second.id].depends_on == second.depends_on
        assert state.nodes[second.id].dependencies == second.dependencies
