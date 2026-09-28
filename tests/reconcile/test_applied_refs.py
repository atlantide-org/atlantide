"""The record of what each ``$ref`` field resolved to when its node was applied.

Covers the record's shape (which fields, which digest scheme, no plaintext), the
plan-side verdicts (:func:`~atlantide.reconcile.applied.consumed`), the apply-side
re-check (:func:`~atlantide.reconcile.reclassify` against the record), and rows
written before the record existed. The end-to-end interrupted-apply scenarios
are in ``test_interrupted_upstream.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, ClassVar

import pytest

from atlantide.core import Lifecycle, Resource, computed, immutable, mutable
from atlantide.core.fields import field_mutability
from atlantide.reconcile import Action, reclassify
from atlantide.reconcile.applied import (
    PLAIN_PREFIX,
    SALTED_PREFIX,
    consumed,
    digest,
    matches,
    ref_digests,
)
from atlantide.reconcile.executor import run as run_module
from atlantide.reconcile.upstream import Consumed
from atlantide.secrets import KeyMaterial, SecretsRegistry
from atlantide.state import NO_INPUT_HASH, MemoryStateBackend, StateGraph
from atlantide.state.codec import StateDocument, dumps
from tests.support import Box, FakeProvider, Harness, Notifier, actions_of, default_outputs

A = "default:test.Box:a"
N = "default:test.Notifier:n"
B = "default:test.Box:b"
K = "default:test.Keyed:k"
C = "default:test.Consumer:c"

SECRET = "hunter2-correct-horse"


class Keyed(Resource):
    """An upstream whose computed ``password`` is ``sensitive``."""

    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable()
    password: str = computed(sensitive=True)
    out: str = computed()


class Consumer(Resource):
    """Consumes an upstream output in a plain field and in a sensitive one."""

    class Meta:
        provider: ClassVar[str] = "test"

    plain: str = mutable(default="")
    token: str = mutable(default="", sensitive=True)


def _stable_updates(_: Any, res: Any) -> dict[str, Any]:
    """An update that keeps every computed output: ``a.out`` stays ``a:<size>``."""
    return default_outputs("create", res)


def _harness(*, stable: bool = False, **kw: Any) -> Harness:
    kw.setdefault("globals", {"Lifecycle": Lifecycle})
    return Harness.of(
        Box,
        Notifier,
        provider=FakeProvider(on_update=_stable_updates if stable else None),
        backend=MemoryStateBackend(),
        **kw,
    )


def _src(size: int = 1, *, label: str = "", message: str = "hello", cbd: bool = False) -> str:
    """``cbd``: ``a`` is create-before-destroy, so replacing it and stopping before
    n and b leaves them untouched. Behind a destroy-first ``a`` the diff replaces
    ``n`` unconditionally and the executor deletes it before ``a``."""
    lifecycle = ", lifecycle=Lifecycle(create_before_destroy=True)" if cbd else ""
    return (
        f"a = Box('a', size={size}, label={label!r}{lifecycle})\n"
        f"Notifier('n', target_arn=a.out, message={message!r})\n"
        "Box('b', size=1, ref=a.out)\n"
    )


def _interrupt_after_a(h: Harness, source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply ``source`` and cancel it once ``a`` is written, before n and b start."""
    original = run_module.ChangeSetRun._apply_node

    async def run() -> None:
        reached = asyncio.Event()

        async def parked(self: Any, node_id: str) -> None:
            if node_id != A:
                reached.set()
                await asyncio.sleep(3600)
            await original(self, node_id)

        monkeypatch.setattr(run_module.ChangeSetRun, "_apply_node", parked)
        task = asyncio.ensure_future(h.apply_async(source, "halt"))
        await asyncio.wait_for(reached.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    monkeypatch.undo()


def _unrecorded(h: Harness, *node_ids: str) -> None:
    """Rewrite rows as a build without the record wrote them."""
    for node_id in node_ids:
        h.backend.put(dataclasses.replace(h.backend.load().nodes[node_id], ref_digests={}))


# -- what a row records ----------------------------------------------------------


def test_a_row_records_each_ref_field_and_nothing_else() -> None:
    h = _harness()
    h.apply(_src())
    rows = h.backend.load().nodes
    secrets = SecretsRegistry()

    assert rows[A].ref_digests == {}  # no refs
    assert set(rows[N].ref_digests) == {"target_arn"}  # `message` is a literal
    assert set(rows[B].ref_digests) == {"ref"}
    expected = digest("target_arn", "a:1", sensitive=False, secrets=secrets)
    assert rows[N].ref_digests["target_arn"] == expected
    assert expected.startswith(PLAIN_PREFIX)


def test_the_plain_digest_does_not_depend_on_the_install() -> None:
    """Another install (another keyfile, or none) reaches the same verdicts."""
    one = digest("f", {"b": [1, 2], "a": "x"}, sensitive=False, secrets=SecretsRegistry())
    other = digest("f", {"a": "x", "b": (1, 2)}, sensitive=False, secrets=SecretsRegistry())
    assert one == other  # canonical: key order and tuple/list do not matter
    assert matches("f", {"a": "x", "b": [1, 2]}, one, SecretsRegistry()) is True
    assert matches("f", {"a": "y", "b": [1, 2]}, one, SecretsRegistry()) is False


def test_an_unknown_scheme_or_undigestible_value_is_no_verdict() -> None:
    secrets = SecretsRegistry()
    assert matches("f", "x", "md5:abc", secrets) is None
    recorded = digest("f", "x", sensitive=False, secrets=secrets)
    assert matches("f", object(), recorded, secrets) is None


def test_a_value_derived_from_a_sensitive_field_is_salted_never_plaintext(tmp_path: Any) -> None:
    """Neither the upstream's sensitive output nor a sensitive consuming field is
    stored in the clear, and their digests use the install salt."""
    material = KeyMaterial(str(tmp_path / "key"))
    secrets = SecretsRegistry(material=material)
    provider = FakeProvider(
        on_create=lambda _, res: (
            {"password": SECRET, "out": "k:1"} if isinstance(res, Keyed) else {}
        )
    )
    h = Harness.of(Keyed, Consumer, provider=provider, secrets=secrets)
    h.apply("k = Keyed('k', size=1)\nConsumer('c', plain=k.password, token=k.out)\n")
    rows = h.backend.load().nodes
    recorded = rows[C].ref_digests

    assert recorded["plain"].startswith(SALTED_PREFIX)  # from a sensitive output
    assert recorded["token"].startswith(SALTED_PREFIX)  # into a sensitive field
    assert recorded["plain"] == digest("plain", SECRET, sensitive=True, secrets=secrets)
    # Another install's salt yields another digest: not a dictionary target.
    other = KeyMaterial(str(tmp_path / "other"))
    other.salt(create=True)  # another install's keyfile: a digest never creates one
    elsewhere = SecretsRegistry(material=other)
    assert recorded["plain"] != digest("plain", SECRET, sensitive=True, secrets=elsewhere)
    raw = dumps(StateDocument(serial=1, nodes=rows))
    assert SECRET.encode() not in raw  # sealed output, digested record
    # And an unchanged config plans nothing: the salted verdict round-trips.
    again = h.diff_only("k = Keyed('k', size=1)\nConsumer('c', plain=k.password, token=k.out)\n")
    assert set(actions_of(again).values()) == {Action.NOOP}


def test_a_collapsed_replace_records_the_values_it_kept() -> None:
    h = _harness(stable=True)
    h.apply(_src())
    before = h.backend.load().nodes[N].ref_digests

    report = h.apply(_src(label="y"))

    assert report.downgraded == {N: "noop"}
    assert h.backend.load().nodes[N].ref_digests == before


def test_ref_digests_skip_what_does_not_resolve() -> None:
    """A property that cannot resolve is left unrecorded, which reads as unknown."""
    secrets = SecretsRegistry()
    properties = {"x": {"$ref": "default:test.Box:gone#out"}, "y": "literal"}
    assert ref_digests("test.Box", properties, {}, types={}, secrets=secrets) == {}


# -- the plan's verdicts -----------------------------------------------------------


def _compiled(h: Harness, source: str) -> Any:
    _, ir, _, _ = h._compile(source, h._providers())
    return ir


def test_consumed_reports_moved_and_unrecorded_fields_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _harness()
    h.apply(_src(cbd=True))
    ir = _compiled(h, _src(cbd=True))
    assert consumed(ir, h.backend.load(), h.secrets) == {}  # every value as applied

    _interrupt_after_a(h, _src(2, cbd=True), monkeypatch)
    ir = _compiled(h, _src(2, cbd=True))
    prior = h.backend.load()
    assert consumed(ir, prior, h.secrets) == {
        N: {"target_arn": Consumed.MOVED},
        B: {"ref": Consumed.MOVED},
    }
    _unrecorded(h, N)
    assert consumed(ir, h.backend.load(), h.secrets)[N] == {"target_arn": Consumed.UNRECORDED}


def test_consumed_ignores_changed_markers_ignored_fields_and_unknown_upstreams() -> None:
    h = _harness()
    h.apply(_src())
    prior = h.backend.load()
    # A changed marker is the diff's business, not a verdict.
    rewired = _compiled(h, "a = Box('a', size=1)\nNotifier('n', target_arn=a.label)\n")
    assert N not in consumed(rewired, prior, h.secrets)
    # An upstream with no confirmed row (being created) yields no verdict.
    orphaned = StateGraph({N: prior.nodes[N]})
    assert consumed(_compiled(h, _src()), orphaned, h.secrets) == {}
    # A missing output yields no verdict either.
    emptied = StateGraph({**prior.nodes, A: dataclasses.replace(prior.nodes[A], outputs={})})
    assert consumed(_compiled(h, _src()), emptied, h.secrets) == {}


def test_ignore_changes_hides_a_moved_value(monkeypatch: pytest.MonkeyPatch) -> None:
    source = (
        "a = Box('a', size={size})\n"
        "Notifier('n', target_arn=a.out, lifecycle=Lifecycle(ignore_changes=['target_arn']))\n"
    )
    h = _harness()
    h.apply(source.format(size=1))
    original = run_module.ChangeSetRun._apply_node

    async def run() -> None:
        reached = asyncio.Event()

        async def parked(self: Any, node_id: str) -> None:
            if node_id == N:
                reached.set()
                await asyncio.sleep(3600)
            await original(self, node_id)

        monkeypatch.setattr(run_module.ChangeSetRun, "_apply_node", parked)
        task = asyncio.ensure_future(h.apply_async(source.format(size=2), "halt"))
        await asyncio.wait_for(reached.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    monkeypatch.undo()

    change = {c.node_id: c for c in h.diff_only(source.format(size=2))}[N]
    assert change.action is not Action.REPLACE
    assert "target_arn" not in change.changed_fields


# -- the apply's re-check ------------------------------------------------------------


def _conditional(h: Harness) -> Any:
    h.apply(_src())
    change = {c.node_id: c for c in h.diff_only(_src(label="y"))}[N]
    assert (change.action, change.conditional) == (Action.REPLACE, True)
    return change


def test_reclassify_compares_with_the_record_not_the_run_start_outputs() -> None:
    """The run-start outputs already hold the moved value (an earlier run moved
    the upstream and stopped); only the record knows the node still has the old one."""
    h = _harness()
    change = _conditional(h)
    secrets = SecretsRegistry()
    recorded = {"target_arn": digest("target_arn", "a:1", sensitive=False, secrets=secrets)}
    moved_everywhere = {"target_arn": "a:2", "message": "hello"}
    muts = field_mutability(Notifier)

    def check(field: str, value: Any, digest_: str) -> bool | None:
        return matches(field, value, digest_, secrets)

    confirmed = reclassify(
        change,
        desired_properties=moved_everywhere,
        prior_properties=moved_everywhere,
        mutability=muts,
        recorded=recorded,
        matches=check,
    )
    assert confirmed is change  # a real replace
    # Without the record, the proxy sees nothing move: the pre-record behaviour.
    proxied = reclassify(
        change,
        desired_properties=moved_everywhere,
        prior_properties=moved_everywhere,
        mutability=muts,
    )
    assert proxied.action is Action.NOOP
    # And a record equal to the resolved value collapses, whatever the proxy says.
    stale_proxy = {"target_arn": "a:0", "message": "hello"}
    collapsed = reclassify(
        change,
        desired_properties={"target_arn": "a:1", "message": "hello"},
        prior_properties=stale_proxy,
        mutability=muts,
        recorded=recorded,
        matches=check,
    )
    assert (collapsed.action, collapsed.changed_fields) == (Action.NOOP, ())


def test_reclassify_falls_back_to_the_proxy_when_a_digest_cannot_be_checked() -> None:
    h = _harness()
    change = _conditional(h)
    same = {"target_arn": "a:1", "message": "hello"}
    fresh = reclassify(
        change,
        desired_properties=same,
        prior_properties=same,
        mutability=field_mutability(Notifier),
        recorded={"target_arn": "md5:unknown"},
        matches=lambda *args: matches(*args, SecretsRegistry()),
    )
    assert fresh.action is Action.NOOP


def test_an_interrupted_value_is_replaced_even_behind_a_changing_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upstream changes again (a tag), keeping the moved value: the plan's
    replace is conditional (its upstream is updating), and the apply confirms it
    against the record rather than collapsing it against the run-start outputs."""
    h = _harness(stable=True)
    h.apply(_src(cbd=True))
    _interrupt_after_a(h, _src(2, cbd=True), monkeypatch)
    h.fake().reset()

    relabelled = _src(2, label="y", cbd=True)
    change = {c.node_id: c for c in h.diff_only(relabelled)}[N]
    assert (change.action, change.conditional) == (Action.REPLACE, True)
    report = h.apply(relabelled)

    assert report.replaced == [N]
    assert report.downgraded == {}
    assert ("delete", "n") in h.fake().calls and ("update", "n") not in h.fake().calls
    assert h.fake().input("create", "n").target_arn == "a:2"  # type: ignore[attr-defined]
    assert set(actions_of(h.diff_only(relabelled)).values()) == {Action.NOOP}


# -- rows written before the record ---------------------------------------------------


def test_an_unrecorded_row_behind_an_unexplained_hash_plans_a_conditional_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing can say whether the value moved, so the plan says it may have:
    conditional for the immutable field, an UPDATE naming the mutable one."""
    h = _harness()
    h.apply(_src(cbd=True))
    _interrupt_after_a(h, _src(2, cbd=True), monkeypatch)
    _unrecorded(h, N, B)

    changes = {c.node_id: c for c in h.diff_only(_src(2, cbd=True))}

    assert (changes[N].action, changes[N].changed_fields) == (Action.REPLACE, ("target_arn",))
    assert changes[N].conditional is True
    assert changes[N].upstream_moved == ()
    assert (changes[B].action, changes[B].changed_fields) == (Action.UPDATE, ("ref",))
    # The apply confirms against the run-start outputs, as before the record:
    # it cannot see the move, and says so by collapsing. No crash, and the rows
    # it writes carry a record from then on.
    report = h.apply(_src(2, cbd=True))
    assert report.downgraded == {N: "noop"}
    rows = h.backend.load().nodes
    assert set(rows[N].ref_digests) == {"target_arn"} and set(rows[B].ref_digests) == {"ref"}
    assert set(actions_of(h.diff_only(_src(2, cbd=True))).values()) == {Action.NOOP}


def test_an_unrecorded_row_is_not_second_guessed_when_something_else_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _harness()
    h.apply(_src(cbd=True))
    _interrupt_after_a(h, _src(2, cbd=True), monkeypatch)
    _unrecorded(h, N)

    change = {c.node_id: c for c in h.diff_only(_src(2, message="bye", cbd=True))}[N]

    assert (change.action, change.changed_fields) == (Action.UPDATE, ("message",))


def test_an_unrecorded_row_behind_the_merkle_skip_or_a_poisoned_hash_is_left_alone() -> None:
    h = _harness()
    h.apply(_src())
    _unrecorded(h, N, B)
    assert set(actions_of(h.diff_only(_src())).values()) == {Action.NOOP}

    poisoned = dataclasses.replace(h.backend.load().nodes[N], input_hash=NO_INPUT_HASH)
    h.backend.put(poisoned)
    change = {c.node_id: c for c in h.diff_only(_src())}[N]
    assert (change.action, change.changed_fields) == (Action.UPDATE, ())


def test_an_upstream_output_folded_in_by_refresh_re_applies_its_consumers() -> None:
    """``refresh --write`` records the upstream's live output; the dependents
    were applied with the old one, so the next plan knows they must follow."""
    h = _harness()
    h.apply(_src())
    h.fake()._live = {
        "a": {"size": 1, "label": "", "out": "a:9"},
        "n": {"target_arn": "a:1", "message": "hello"},
        "b": {"size": 1, "label": "", "ref": "a:1", "out": "b:1"},
    }
    h.refresh(write=True)
    assert h.backend.load().nodes[A].outputs == {"out": "a:9"}

    changes = {c.node_id: c for c in h.diff_only(_src())}

    assert changes[A].action is Action.NOOP
    assert (changes[N].action, changes[N].upstream_moved) == (Action.REPLACE, ("target_arn",))
    assert (changes[B].action, changes[B].changed_fields) == (Action.UPDATE, ("ref",))


def test_an_install_that_cannot_open_the_upstream_outputs_reports_nothing(
    tmp_path: Any,
) -> None:
    """A foreign keyfile cannot unseal a sensitive upstream output: no verdict,
    so no false "moved" (and no replace) on that install's plan."""
    secrets = SecretsRegistry(material=KeyMaterial(str(tmp_path / "key")))
    provider = FakeProvider(
        on_create=lambda _, res: (
            {"password": SECRET, "out": "k:1"} if isinstance(res, Keyed) else {}
        )
    )
    h = Harness.of(Keyed, Consumer, provider=provider, secrets=secrets)
    source = "k = Keyed('k', size=1)\nConsumer('c', plain=k.password)\n"
    h.apply(source)
    ir = _compiled(h, source)
    assert consumed(ir, h.backend.load(), secrets) == {}

    foreign = SecretsRegistry(material=KeyMaterial(str(tmp_path / "other")))
    assert consumed(ir, h.backend.load(), foreign) == {}


def test_reclassify_treats_a_recorded_field_dropped_from_config_as_changed() -> None:
    h = _harness()
    change = _conditional(h)
    secrets = SecretsRegistry()
    recorded = {"target_arn": digest("target_arn", "a:1", sensitive=False, secrets=secrets)}
    fresh = reclassify(
        change,
        desired_properties={"message": "hello"},
        prior_properties={"target_arn": "a:1", "message": "hello"},
        mutability=field_mutability(Notifier),
        recorded=recorded,
        matches=lambda *args: matches(*args, secrets),
    )
    assert fresh is change  # an immutable field went away: still a replace
