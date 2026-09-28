"""``atlantide.testing``: every API method, against the engine's own stages."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from returns.pipeline import is_successful
from returns.result import Failure

from atlantide.core import (
    AtlantideError,
    PreventDestroyError,
    ProviderRegistry,
    RegistryError,
    Stack,
    collecting,
    current_stack,
    current_stack_region,
)
from atlantide.core.plugin import Discovery, PluginError
from atlantide.engine import Engine
from atlantide.providers.loader import discover
from atlantide.providers.local import File, Null
from atlantide.reconcile import type_mutability
from atlantide.state import MemoryStateBackend, NodeStatus
from atlantide.testing import Action, Change, ChangeSet, Compiled, Plan, local_names, stack
from atlantide.testing import compiled as module
from tests.support.resources import Box
from tests.testing.site import CORE, NAME, OPTIONAL, REGION, Site


def site(**kwargs: Any) -> Compiled:
    """One ``Site`` with the test defaults plus ``kwargs``, compiled."""
    return Compiled.of(lambda: Site(NAME, **kwargs), region=REGION)


# --- building -------------------------------------------------------------


def test_stack_opens_a_stack_with_the_defaults() -> None:
    with stack(region=REGION) as opened:
        assert isinstance(opened, Stack)
        assert (current_stack(), current_stack_region()) == ("infra", REGION)
        page = Site(NAME).page
    assert page.node_id == "infra:local.File:site-page"


def test_stack_takes_a_name_prefix_and_tags() -> None:
    with stack("prod", region=REGION, name_prefix=None, tags={"team": "web"}) as opened:
        assert current_stack() == "prod"
    assert (opened.name_prefix, opened.tags) == (None, {"team": "web"})


def test_local_names_namespace_children_under_the_instance() -> None:
    assert local_names("page", "token", name="site") == {"site-page", "site-token"}
    assert local_names(name="site") == frozenset()


# --- compiling ------------------------------------------------------------


def test_of_lowers_every_child() -> None:
    compiled = site()
    assert compiled.names == local_names(*CORE, name=NAME)
    assert compiled["site-page"].type == "local.File"
    assert compiled["site-page"].properties == {"path": "index.html", "content": "hello"}
    assert set(compiled.hashes) == {node.id for node in compiled.ir.nodes}


def test_of_passes_stack_region_prefix_and_tags() -> None:
    seen: dict[str, Any] = {}

    def build() -> None:
        seen["stack"], seen["region"] = current_stack(), current_stack_region()
        Null("n")

    compiled = Compiled.of(build, stack="prod", region="us-east-1", tags={"a": "b"})
    assert seen == {"stack": "prod", "region": "us-east-1"}
    assert compiled.names == {"n"}
    assert compiled["n"].id == "prod:local.Null:n"


def test_two_evaluations_are_byte_identical() -> None:
    first, second = site(), site()
    assert first.bytes == second.bytes
    assert first.hashes == second.hashes
    assert first == second


def test_a_changed_input_moves_the_bytes_and_the_hash() -> None:
    before, after = site(), site(content="bye")
    assert before.bytes != after.bytes
    assert before.hashes != after.hashes


def test_two_instances_lower_to_disjoint_nodes() -> None:
    both = Compiled.of(lambda: [Site("dev"), Site("prod")], region=REGION)
    assert both.names == local_names(*CORE, name="dev") | local_names(*CORE, name="prod")


def test_hashes_match_the_engines_compile() -> None:
    """The same resources compiled from Atlas-lang source hash identically."""
    source = (
        "from atlantide.core import Stack\n"
        "from atlantide.providers.local import File, Null\n"
        "with Stack('infra', region='eu-north-1', name_prefix='acme'):\n"
        "    page = File('page', path='index.html', content='hello')\n"
        "    Null('marker', triggers={'sum': page.checksum})\n"
    )

    def build() -> None:
        page = File("page", path="index.html", content="hello")
        Null("marker", triggers={"sum": page.checksum})

    engine = Engine(ProviderRegistry(), MemoryStateBackend(), discover().types())
    via_engine = engine.compile(source).unwrap()
    ours = Compiled.of(build, region=REGION)
    assert ours.hashes == via_engine.hashes
    assert ours.bytes == Compiled(via_engine.ir, via_engine.hashes).bytes


def test_getitem_rejects_an_unknown_name() -> None:
    with pytest.raises(KeyError, match="no node is named 'site-nope'"):
        site()["site-nope"]


def test_getitem_rejects_an_ambiguous_name() -> None:
    compiled = Compiled.of(lambda: [File("x", path="x"), Null("x")], region=REGION)
    with pytest.raises(KeyError, match="several nodes are named 'x'"):
        compiled["x"]


def test_of_raises_the_compile_error() -> None:
    """A registry the engine refuses to lower raises its ``AtlantideError``."""
    with collecting(), Stack("other", region=REGION):
        outside = File("outside", path="o")
    with pytest.raises(AtlantideError, match="unknown node"):
        Compiled.of(lambda: Null("n", triggers={"x": outside.checksum}), region=REGION)


def test_state_is_what_an_apply_commits() -> None:
    compiled = site(protect=True)
    state = compiled.state()
    assert set(state.nodes) == {node.id for node in compiled.ir.nodes}
    page = state.nodes[compiled["site-page"].id]
    assert page.input_hash == compiled.hashes[page.id]
    assert page.status == NodeStatus.CREATED
    assert page.prevent_destroy is True
    assert page.properties == compiled["site-page"].properties
    token = state.nodes[compiled["site-token"].id]
    assert token.dependencies == (page.id,)
    assert token.prevent_destroy is False


def test_empty_declares_nothing() -> None:
    empty = Compiled.empty()
    assert empty.names == frozenset()
    assert empty.state().nodes == {}


# --- planning -------------------------------------------------------------


def test_first_run_creates_every_node() -> None:
    run = site().against()
    assert isinstance(run, Plan)
    assert isinstance(run.changes, ChangeSet)
    assert set(run.actions) == local_names(*CORE, name=NAME)
    assert set(run.actions.values()) == {Action.CREATE}
    assert run.protected == frozenset()


def test_unchanged_config_is_all_noop() -> None:
    assert set(site().against(site()).actions.values()) == {Action.NOOP}


def test_a_mutable_change_updates_in_place() -> None:
    run = site(content="bye").against(site())
    assert run.actions["site-page"] is Action.UPDATE
    change = run["site-page"]
    assert isinstance(change, Change)
    assert change.changed_fields == ("content",)


def test_an_immutable_change_replaces() -> None:
    run = site(path="about.html").against(site())
    assert run.actions["site-page"] is Action.REPLACE
    assert run["site-page"].changed_fields == ("path",)
    assert run["site-page"].conditional is False


def test_an_immutable_ref_replaces_conditionally() -> None:
    """The token's ``keepers`` holds a ``Ref`` to the page, resolved only at apply."""
    token = site(content="bye").against(site())["site-token"]
    assert token.action is Action.REPLACE
    assert token.conditional is True
    assert token.changed_fields == ("keepers",)


def test_an_optional_block_creates_then_deletes_only_itself() -> None:
    on = site(marker=True).against(site()).actions
    assert on == {
        **dict.fromkeys(local_names(*CORE, name=NAME), Action.NOOP),
        "site-marker": Action.CREATE,
    }

    off = site().against(site(marker=True))
    assert off.actions["site-marker"] is Action.DELETE
    assert off["site-marker"].desired is None
    assert {off.actions[name] for name in local_names(*CORE, name=NAME)} == {Action.NOOP}
    assert local_names(*OPTIONAL, name=NAME) <= set(off.actions)


def test_plan_getitem_rejects_an_unknown_name() -> None:
    with pytest.raises(KeyError, match="no node is named 'site-nope'"):
        site().against()["site-nope"]


# --- destroy guard --------------------------------------------------------


def test_teardown_deletes_everything_and_approves_unprotected() -> None:
    prior = site()
    run = Compiled.empty().against(prior)
    assert set(run.actions.values()) == {Action.DELETE}
    assert run.protected == frozenset()
    assert run.approve().unwrap() is run.changes


def test_teardown_is_refused_while_protected() -> None:
    prior = site(protect=True)
    run = Compiled.empty().against(prior)
    assert run.protected == {prior["site-page"].id}

    verdict = run.approve()
    assert isinstance(verdict, Failure)
    error = verdict.failure()
    assert isinstance(error, PreventDestroyError)
    assert prior["site-page"].id in str(error)


def test_replacing_a_protected_node_is_refused() -> None:
    run = site(path="about.html", protect=True).against(site(protect=True))
    assert run.actions["site-page"] is Action.REPLACE
    assert not is_successful(run.approve())


def test_dropping_protect_changes_nothing_and_reopens_teardown() -> None:
    """``prevent_destroy`` is not hashed: dropping it is a state-only NOOP whose
    apply reopens teardown."""
    dropped = site().against(site(protect=True))
    assert set(dropped.actions.values()) == {Action.NOOP}
    assert dropped.state_only == {"site-page"}
    assert is_successful(Compiled.empty().against(dropped).approve())
    assert is_successful(Compiled.empty().against(site()).approve())


# --- mutability -----------------------------------------------------------


@pytest.fixture
def fresh_discovery() -> Iterator[None]:
    """Clear the process-wide discovery cache around a test that patches it."""
    module._installed_mutability.cache_clear()
    yield
    module._installed_mutability.cache_clear()


def test_default_mutability_is_the_engines() -> None:
    """The table ``Engine`` passes to ``diff`` for the installed plugins."""
    engine = Engine(ProviderRegistry(), MemoryStateBackend(), discover().types())
    assert site().mutability == engine.mutability
    assert Compiled.of(lambda: None, region=REGION).mutability == engine.mutability


def test_explicit_types_replace_the_installed_ones() -> None:
    """A type no installed plugin declares plans once its types are passed."""
    prior = Compiled.of(lambda: Box("b", size=1), region=REGION, types={"test.Box": Box})
    assert prior.mutability == type_mutability({"test.Box": Box})
    desired = Compiled.of(lambda: Box("b", size=2), region=REGION, types={"test.Box": Box})
    assert desired.against(prior).actions == {"b": Action.REPLACE}


def test_an_unknown_type_is_refused() -> None:
    """Without its mutability, every changed field would read as mutable."""
    with pytest.raises(RegistryError, match=r"'test\.Box'"):
        Compiled.of(lambda: Box("b", size=1), region=REGION)


@pytest.mark.usefixtures("fresh_discovery")
def test_a_contested_plugin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    contested = Discovery(errors=(PluginError("acme", "claimed twice", fatal=True),))
    monkeypatch.setattr(module, "discover", lambda: contested)
    with pytest.raises(RegistryError, match="'acme' could not be registered: claimed twice"):
        Compiled.of(lambda: None, region=REGION)


@pytest.mark.usefixtures("fresh_discovery")
def test_a_plugin_that_failed_to_load_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = Discovery(errors=(PluginError("acme", "ImportError: nope"),))
    monkeypatch.setattr(module, "discover", lambda: broken)
    assert Compiled.of(lambda: None, region=REGION).mutability == {}


# --- prevent_destroy changes and the committed state ---------------------------


def test_adding_protect_is_a_state_only_change() -> None:
    run = site(protect=True).against(site())
    assert set(run.actions.values()) == {Action.NOOP}
    assert run["site-page"].state_only is True
    assert run.state_only == {"site-page"}
    assert site().against(site()).state_only == frozenset()


def test_protect_added_in_the_same_plan_refuses_a_replace() -> None:
    """The guard reads the flag the config declares, not the one state holds."""
    run = site(path="about.html", protect=True).against(site())
    assert run.actions["site-page"] is Action.REPLACE
    assert run.protected == {site()["site-page"].id}
    assert isinstance(run.approve().failure(), PreventDestroyError)


def test_protect_removed_in_the_same_plan_permits_a_replace() -> None:
    run = site(path="about.html").against(site(protect=True))
    assert run.protected == frozenset()
    assert is_successful(run.approve())


def test_committed_is_the_state_the_apply_would_leave() -> None:
    prior = site()
    run = site(protect=True, content="bye").against(prior)
    committed = run.committed()
    wanted = site(protect=True, content="bye")
    # Written nodes (the page's UPDATE, the token's REPLACE) are the new rows.
    assert committed == wanted.state()
    assert committed.nodes[wanted["site-page"].id].prevent_destroy is True


def test_committed_keeps_noop_rows_and_records_a_state_only_flag() -> None:
    prior = site()
    run = site(protect=True).against(prior)
    committed = run.committed()
    page_id = prior["site-page"].id
    assert committed.nodes[page_id].prevent_destroy is True
    assert committed.nodes[page_id].input_hash == prior.state().nodes[page_id].input_hash
    token_id = prior["site-token"].id
    assert committed.nodes[token_id] == prior.state().nodes[token_id]


def test_committed_drops_deleted_nodes() -> None:
    run = site().against(site(marker=True))
    assert set(run.committed().nodes) == {node.id for node in site().ir.nodes}


def test_a_plan_can_be_planned_over() -> None:
    """``against(plan)`` plans over that plan's committed state: a teardown after
    a protect-only apply is refused."""
    protected = site(protect=True).against(site())
    teardown = Compiled.empty().against(protected)
    assert isinstance(teardown.approve().failure(), PreventDestroyError)
    assert set(site(protect=True).against(protected).actions.values()) == {Action.NOOP}
    assert site(protect=True).against(protected).state_only == frozenset()
    assert Compiled.empty().against(protected.committed()).protected == protected.protected
