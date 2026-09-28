"""Deterministic races against the S3 journal backend (design §8).

Every test here scripts one interleaving with :mod:`tests.state.s3.harness`:
a thread is parked just before or after a chosen AWS call, the competing work
runs, and the parked thread is let go. What is asserted is the invariant the
interleaving threatens — atomic fencing (I2), per-node order (I3), crash safety
(I5), reader consistency (I6) — read back through a fresh backend, i.e. another
process.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from atlantide.core import is_successful
from atlantide.core.errors import FencedWriteError
from atlantide.state.codec import (
    EntryKind,
)
from atlantide.state.s3 import limits
from atlantide.state.s3.journal import head_of_item
from tests.support import FakeClock

from ..conftest import LOCK_TABLE, node
from .harness import Fault, SimulatedCrash, crashing, in_thread, joined, stepping
from .support import (
    KEY,
    TAKEOVER,
    bound_lease,
    ddb_client,
    journal_layout,
    new_backend,
)


def _value(node_id: str) -> str | None:
    """``node_id``'s input hash as another process reads it (``None``: absent)."""
    found = new_backend().load().nodes.get(node_id)
    return found.input_hash if found is not None else None


def _is_commit(kwargs: dict[str, Any]) -> bool:
    """A node write's head commit (not a fence bump or a head rebuild)."""
    return "ref" in str(kwargs.get("UpdateExpression", "")) or ":ref" in kwargs.get(
        "ExpressionAttributeValues", {}
    )


def _is_renewal(kwargs: dict[str, Any]) -> bool:
    """A lease renewal (the one update of the lease item)."""
    return "lease_expires_at" in str(kwargs.get("ExpressionAttributeNames", {}))


def _is_entry(kwargs: dict[str, Any]) -> bool:
    return ".d/" in str(kwargs.get("Key", ""))


def _is_snapshot(kwargs: dict[str, Any]) -> bool:
    return kwargs.get("Key") == KEY


def _wipe_table() -> None:
    """A recreated lock table: every lock row, head and the fence counter gone."""
    ddb = ddb_client()
    for page in ddb.get_paginator("scan").paginate(TableName=LOCK_TABLE):
        for item in page.get("Items", []):
            ddb.delete_item(TableName=LOCK_TABLE, Key={"node_id": item["node_id"]})


def test_a_commit_landing_while_the_view_is_re_read_is_absorbed_into_it(aws: None) -> None:
    """A bulk write drops the view, a reader starts re-reading it, and a
    concurrent single-node commit lands after the reader's journal read. The
    commit must survive into the installed view."""
    backend = new_backend()
    backend.put(node("x"))
    s3, ddb = stepping(backend)
    commit = ddb.pause("update_item", _is_commit, thread="writer")
    reread = s3.pause("head_object", thread="reader")

    writer = in_thread(lambda: backend.put(node("y")), "writer")
    commit.wait()
    backend.put_many([node("p"), node("q")])  # a snapshot write: drops the view
    reader = in_thread(lambda: backend.load(), "reader")
    reread.wait()  # read the journal before y's commit
    commit.release()
    joined(*writer)
    reread.release()
    assert set(joined(*reader).nodes) == {"x", "y", "p", "q"}
    assert backend.serial() == new_backend().serial()


# -- view reloads and fence reseeds --------------------------------------------


def test_a_commit_made_while_another_writer_reloads_the_view_is_not_forgotten(
    aws: None,
) -> None:
    """Two first writes on a fresh state. Writer 1 still holds the epoch-less view
    and, finding the snapshot now exists, drops the view writer 2 installed and
    re-reads. Writer 2 commits while the view is dropped, so its local update has
    nowhere to go; writer 1 then installs a view read *before* that commit.

    Guards against the run's own write missing from its view, which would make
    a later delete of that node a no-op that leaves the node in place.
    """
    backend = new_backend()
    backend.load()  # the view, before any snapshot exists: no epoch
    s3, ddb = stepping(backend)
    first_read = s3.pause("get_object", thread="w1")
    reread_done = s3.pause("head_object", thread="w1")
    commit = ddb.pause("update_item", _is_commit, thread="w2")

    w1 = in_thread(lambda: backend.put(node("x")), "w1")
    first_read.wait()  # w1 holds the epoch-less view
    w2 = in_thread(lambda: backend.put(node("y")), "w2")
    commit.wait()  # w2 created the snapshot, installed its view, stored its entry
    first_read.release()
    if reread_done.reached_before(w1[0]):
        # w1 dropped w2's view and re-read the journal before y's commit
        commit.release()
        joined(*w2)  # y committed while the view was dropped
        reread_done.release()
        joined(*w1)
    else:
        joined(*w1)
        commit.release()
        joined(*w2)

    assert "y" in backend.load().nodes, "this run's own commit vanished from its view"
    backend.delete("y")
    assert _value("y") is None, "a delete that returned left the node in place"


def test_a_crash_while_reseeding_the_fence_counter_cannot_reissue_a_held_fence(
    aws: None,
) -> None:
    """The lock table is recreated while run P holds a fence minted after the last
    fold. The first acquire after that re-seeds the counter far above the
    snapshot, in two calls. If it dies between them, the next acquire must not
    take the existing counter as seeded and mint P's fence again, or P's paused
    writes would land under the new holder's lease (I2)."""
    clock = FakeClock()
    setup = new_backend(clock=clock)
    for _ in range(3):
        setup.acquire_lock("setup", 60.0, frozenset({"a"})).unwrap()
        setup.release_lock("setup")
    setup.compact()  # the snapshot now records the highest fence so far

    paused = new_backend(clock=clock)
    held = bound_lease(paused, "paused", {"a"})
    paused.load()
    _wipe_table()
    clock.advance(TAKEOVER)

    # Dies right after its first counter update: whatever that update did is
    # all the re-seed there is.
    dying = crashing(new_backend(clock=clock), Fault("update_item", 1, after=True))
    with pytest.raises(SimulatedCrash):
        dying.acquire_lock("dying", 60.0, frozenset({"a"}))

    taker = new_backend(clock=clock)
    taken = bound_lease(taker, "taker", {"a"})
    assert taken.fence != held.fence
    with pytest.raises(FencedWriteError):
        paused.put(node("a", input_hash="stale"))
    assert _value("a") is None


def test_rebuilding_a_lost_head_prefers_the_newer_fence_on_a_seq_tie(aws: None) -> None:
    """A stale writer's refused entry and the new holder's committed one can share
    a seq. With the head lost, ``fsck --rebuild-heads`` must not resurrect the
    refused write because its key happens to list first."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a"})
    run_a.put(node("a", input_hash="from-a"))
    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"a"})
    run_b.put(node("a", input_hash="from-b"))  # seq 2
    with pytest.raises(FencedWriteError):
        run_a.put(node("a", input_hash="stale-from-a"))  # also seq 2, refused

    head_key = journal_layout().head_key(EntryKind.NODE, "a")
    ddb_client().delete_item(TableName=LOCK_TABLE, Key={"node_id": {"S": head_key}})
    report = new_backend().fsck(rebuild_heads=True)
    assert [item for item, _ in report.rebuilt] == ["a"]
    assert _value("a") == "from-b"
    item = ddb_client().get_item(TableName=LOCK_TABLE, Key={"node_id": {"S": head_key}})["Item"]
    assert head_of_item(item).seq == 2


def test_fsck_after_a_crash_before_a_first_commit_resurrects_nothing(aws: None) -> None:
    """A run dies between storing a new node's first entry and committing it. The
    head holds the run's fence but no seq — the same shape as a head lost with a
    recreated table. It is a pending write, not a lost head: fsck must call the
    store healthy, and ``--rebuild-heads`` must not commit what never was."""
    clock = FakeClock()
    dying = crashing(new_backend(clock=clock), Fault("update_item", 2))  # fence bump, commit
    dying.bind_lease(dying.acquire_lock("dying", 60.0, frozenset({"a"})).unwrap())
    with pytest.raises(SimulatedCrash):
        dying.put(node("a", input_hash="never-committed"))

    report = new_backend().fsck()
    assert report.healthy and len(report.pending) == 1
    assert new_backend().fsck(rebuild_heads=True).rebuilt == []
    assert _value("a") is None


def test_fsck_still_rebuilds_a_head_lost_with_the_table_and_re_fenced(aws: None) -> None:
    """The case the rule above must not swallow: the table was recreated and the
    node re-acquired since, so its head has a (much newer) fence and no seq."""
    clock = FakeClock()
    first = new_backend(clock=clock)
    first.bind_lease(first.acquire_lock("first", 60.0, frozenset({"a"})).unwrap())
    first.put(node("a", input_hash="committed"))
    first.compact()  # max_fence recorded: the re-seed lands far above it
    first.put(node("a", input_hash="committed-after-the-fold"))
    _wipe_table()
    clock.advance(TAKEOVER)
    new_backend(clock=clock).acquire_lock("next", 60.0, frozenset({"a"})).unwrap()

    report = new_backend().fsck(rebuild_heads=True)
    assert report.headless == ["a"] and len(report.rebuilt) == 1
    assert _value("a") == "committed-after-the-fold"


def test_a_rebased_write_keeps_the_cached_serial_in_step_with_the_store(aws: None) -> None:
    """A write that finds its head moved (another writer's commits) rebases onto
    it. The serial this backend then reports must count those commits too, or it
    disagrees with every other process about the same content."""
    ours, theirs = new_backend(), new_backend()
    ours.put(node("a", input_hash="1"))
    ours.set_outputs({"dev:x": 1})
    theirs.load()
    theirs.put(node("a", input_hash="2"))
    theirs.put(node("a", input_hash="3"))
    theirs.set_outputs({"dev:x": 2})
    theirs.set_outputs({"dev:x": 3})

    ours.put(node("a", input_hash="4"))  # rebases over two commits
    ours.set_outputs({"dev:y": 1})  # and over two output commits
    assert ours.serial() == new_backend().serial()
    assert ours.outputs() == new_backend().outputs() == {"dev:x": 3, "dev:y": 1}


# -- a stale writer across a takeover ------------------------------------------


def test_a_writer_parked_before_its_commit_is_refused_after_a_takeover(aws: None) -> None:
    """Ordering 1: A's entry is stored, B takes over, A's commit arrives. Refused
    atomically at the head; B never sees A's value, and B's own writes land."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a"})
    run_a.put(node("a", input_hash="from-a"))
    _, ddb = stepping(run_a)
    commit = ddb.pause("update_item", _is_commit)

    stale = in_thread(lambda: run_a.put(node("a", input_hash="stale")), "stale")
    commit.wait()
    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"a"})
    assert run_b.load().nodes["a"].input_hash == "from-a"
    commit.release()
    with pytest.raises(FencedWriteError, match="run-b"):
        joined(*stale)

    assert _value("a") == "from-a"
    run_b.put(node("a", input_hash="from-b"))
    assert _value("a") == "from-b"
    assert new_backend().fsck().healthy


def test_a_writer_whose_commit_landed_before_the_takeover_is_seen_by_the_new_holder(
    aws: None,
) -> None:
    """Ordering 2: A's commit lands (its response still in flight), then B takes
    over. The commit linearized before the grant, so B's post-acquire read sees
    it (I6), and A's next write is refused (I2)."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a"})
    _, ddb = stepping(run_a)
    landed = ddb.pause("update_item", _is_commit, after=True)

    late = in_thread(lambda: run_a.put(node("a", input_hash="late-from-a")), "late")
    landed.wait()
    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"a"})
    assert run_b.load().nodes["a"].input_hash == "late-from-a"
    landed.release()
    joined(*late)

    with pytest.raises(FencedWriteError):
        run_a.put(node("a", input_hash="after-the-takeover"))
    run_b.put(node("a", input_hash="from-b"))
    assert _value("a") == "from-b"


def test_a_renewal_racing_a_takeover_and_release_reports_the_loss(aws: None) -> None:
    """A's renewal is in flight while B takes the node and releases it again: the
    lock row is then gone, but B revoked A's lease item before taking the node,
    so the renewal (one update of that item) fails and A can write nothing more."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a"})
    run_a.put(node("a", input_hash="from-a"))
    _, ddb = stepping(run_a)
    renewing = ddb.pause("update_item", _is_renewal)

    renewal = in_thread(lambda: run_a.renew_lock("run-a", 60.0, frozenset({"a"})), "renew")
    renewing.wait()
    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"a"})
    run_b.put(node("a", input_hash="from-b"))
    run_b.bind_lease(None)
    run_b.release_lock("run-b")
    renewing.release()

    renewed = joined(*renewal)
    assert not is_successful(renewed)
    assert "newer lease" in str(renewed.failure())
    assert run_a.locks() == {}, "the failed renewal took no lock row"
    with pytest.raises(FencedWriteError):
        run_a.put(node("a", input_hash="stale"))
    assert _value("a") == "from-b"


def test_stale_outputs_are_refused_after_a_takeover_and_release(aws: None) -> None:
    """Output heads are fenced through the lease's node heads in the stack
    (deviation 1): a takeover-and-release in between is still caught."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"dev:t:a"})
    run_a.set_outputs({"dev:url": "a"})
    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"dev:t:a"})
    run_b.set_outputs({"dev:url": "b"})
    run_b.bind_lease(None)
    run_b.release_lock("run-b")

    with pytest.raises(FencedWriteError):
        run_a.set_outputs({"dev:url": "stale"})
    assert new_backend().outputs() == {"dev:url": "b"}


# -- compaction racing commits and readers ---------------------------------------


def test_compaction_never_collects_an_entry_waiting_for_its_commit(aws: None) -> None:
    """The pending-orphan rule: a writer between its entry PUT and its commit must
    find its entry still there, or its head would name a missing object."""
    backend = new_backend()
    bound_lease(backend, "me", {"a"})
    backend.put(node("a", input_hash="v1"))
    new_backend().compact()
    _, ddb = stepping(backend)
    commit = ddb.pause("update_item", _is_commit)

    writer = in_thread(lambda: backend.put(node("a", input_hash="v2")), "writer")
    commit.wait()
    new_backend().compact()
    commit.release()
    joined(*writer)

    assert _value("a") == "v2"
    assert new_backend().fsck().healthy
    new_backend().compact()
    assert _value("a") == "v2"


def test_a_commit_between_the_compaction_cut_and_its_swap_stays_in_the_journal(
    aws: None,
) -> None:
    backend = new_backend()
    backend.put(node("a", input_hash="v1"))
    backend.put(node("b", input_hash="v1"))
    compactor = new_backend()
    s3, _ = stepping(compactor)
    swap = s3.pause("put_object", _is_snapshot)

    folding = in_thread(compactor.compact, "compactor")
    swap.wait()
    backend.put(node("a", input_hash="v2"))  # after the cut, before the swap
    serial = new_backend().serial()
    swap.release()
    report = joined(*folding)

    assert not report.skipped and report.folded == 2
    assert (_value("a"), _value("b")) == ("v2", "v1")
    assert new_backend().serial() == serial, "a fold never moves the serial"
    assert new_backend().fsck().healthy
    assert new_backend().compact().folded == 1
    assert (_value("a"), _value("b")) == ("v2", "v1")


def test_a_reader_racing_a_commit_and_a_compaction_restarts_onto_a_consistent_view(
    aws: None,
) -> None:
    """The reader has read the heads (a at v1) when a commits v2 and a compaction
    folds it and deletes both entries. The reader's fetch of v1 meets a deleted
    object and restarts: it returns v2, never a mix or an error."""
    writer = new_backend()
    writer.put(node("a", input_hash="v1"))
    writer.put(node("b", input_hash="v1"))
    reader = new_backend()
    _, ddb = stepping(reader)
    heads_read = ddb.pause("batch_get_item", after=True)

    reading = in_thread(reader.load, "reader")
    heads_read.wait()
    writer.put(node("a", input_hash="v2"))
    assert new_backend().compact().deleted == 3
    heads_read.release()
    graph = joined(*reading)

    assert (graph.nodes["a"].input_hash, graph.nodes["b"].input_hash) == ("v2", "v1")
    assert reader.serial() == new_backend().serial()


def test_two_compactors_at_once_one_folds_and_the_other_changes_nothing(aws: None) -> None:
    backend = new_backend()
    backend.put(node("a", input_hash="v1"))
    backend.put(node("b", input_hash="v1"))
    first, second = new_backend(), new_backend()
    s3, _ = stepping(first)
    swap = s3.pause("put_object", _is_snapshot)

    folding = in_thread(first.compact, "first")
    swap.wait()
    backend.put(node("a", input_hash="v2"))
    assert second.compact().folded == 2
    swap.release()
    lost = joined(*folding)

    assert lost.skipped and lost.deleted == 0
    assert (_value("a"), _value("b")) == ("v2", "v1")
    assert new_backend().fsck().healthy


def test_a_compactor_deleting_late_cannot_touch_a_newer_fold(aws: None) -> None:
    """The first compactor swapped the snapshot and is slow to delete; meanwhile a
    newer commit is made and folded by a second compactor. The late deletes only
    cover what the first fold superseded."""
    backend = new_backend()
    backend.put(node("a", input_hash="v1"))
    first = new_backend()
    s3, _ = stepping(first)
    deleting = s3.pause("delete_objects")

    folding = in_thread(first.compact, "first")
    deleting.wait()
    backend.put(node("a", input_hash="v2"))
    backend.put(node("a", input_hash="v3"))
    new_backend().compact()
    backend.put(node("a", input_hash="v4"))  # a live, unfolded commit
    deleting.release()
    joined(*folding)

    assert _value("a") == "v4"
    assert new_backend().fsck().healthy


def test_compaction_by_a_run_whose_lease_was_lost_folds_only_committed_state(
    aws: None,
) -> None:
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a"})
    run_a.put(node("a", input_hash="from-a"))
    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"a"})
    run_b.put(node("a", input_hash="from-b"))
    with pytest.raises(FencedWriteError):
        run_a.put(node("a", input_hash="stale"))

    run_a.checkpoint()
    assert _value("a") == "from-b"
    assert new_backend().fsck().healthy


# -- readers ------------------------------------------------------------------


def test_a_locked_reader_sees_every_prior_commit_even_when_relists_run_out(
    aws: None,
) -> None:
    """Deviation 6: an unrelated node committed after every LIST keeps the
    causality guard unsatisfied until it gives up. A *locked* reader still sees
    every commit made in its scope before its acquire: those entries existed
    before its first LIST."""
    clock = FakeClock()
    writer = new_backend(clock=clock)
    bound_lease(writer, "writer", {"a"})
    writer.put(node("a", input_hash="committed"))
    writer.bind_lease(None)
    writer.release_lock("writer")
    churn = new_backend()
    churn.put(node("z0", input_hash="v1"))
    listed = [0]

    def outrun(_: Any) -> None:
        # Move the head of a node this LIST just showed (the reader has not read
        # it yet), and start the next one for the next LIST to show.
        nth = listed[0]
        listed[0] += 1
        churn.put(node(f"z{nth}", input_hash="v2"))
        churn.put(node(f"z{nth + 1}", input_hash="v1"))

    reader = new_backend(clock=clock)
    bound_lease(reader, "reader", {"a"})
    s3, _ = stepping(reader)
    s3.every("list_objects_v2", outrun)

    graph = reader.load()
    listings = [name for name, _ in s3.calls if name == "list_objects_v2"]
    assert len(listings) == 1 + limits.RELIST_ATTEMPTS, "the guard ran out"
    assert graph.nodes["a"].input_hash == "committed"
    assert graph.nodes["z0"].input_hash == "v2"


# -- bulk writes -------------------------------------------------------------


def test_a_takeover_before_the_bulk_pre_check_refuses_the_bulk(aws: None) -> None:
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a", "b"})
    run_a.put(node("a", input_hash="from-a"))
    _, ddb = stepping(run_a)
    cut = ddb.pause("batch_get_item")  # the bulk's cut, before its pre-check

    bulk = in_thread(
        lambda: run_a.put_many([node("a", input_hash="bulk"), node("b", input_hash="bulk")]),
        "bulk",
    )
    cut.wait()
    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"a", "b"})
    cut.release()
    with pytest.raises(FencedWriteError):
        joined(*bulk)
    assert (_value("a"), _value("b")) == ("from-a", None)


@pytest.mark.xfail(
    strict=True,
    reason="documented gap (design 4.6, owner Q2): the bulk fence check is a "
    "pre-check, not atomic with the snapshot swap",
)
def test_a_takeover_between_the_bulk_pre_check_and_its_swap_is_refused(aws: None) -> None:
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a", "b"})
    s3, _ = stepping(run_a)
    swap = s3.pause("put_object", _is_snapshot)

    bulk = in_thread(
        lambda: run_a.put_many([node("a", input_hash="bulk"), node("b", input_hash="bulk")]),
        "bulk",
    )
    swap.wait()
    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"a", "b"})
    swap.release()
    with pytest.raises(FencedWriteError):
        joined(*bulk)


# -- the cache under concurrent writers -----------------------------------------


def test_sixteen_writers_and_background_compaction_keep_the_view_exact(aws: None) -> None:
    """The executor's pool (16 writers, a write-ahead and a persist per node),
    background folds every few commits, and reads in between: afterwards this
    backend's cached view, a fresh reader and the serial all agree."""
    backend = new_backend(compact_every=7)
    bound_lease(backend, "run", {f"n{i}" for i in range(48)} | {"old"})

    def lifecycle(i: int) -> None:
        backend.put(node(f"n{i}", input_hash="creating"))
        backend.load()
        backend.put(node(f"n{i}", input_hash="created"))
        if i % 3 == 0:
            backend.delete(f"n{i}")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lifecycle, range(48)))
    cached, cached_serial = backend.load(), backend.serial()  # maintained by the writes
    backend.close()  # joins the background compactor

    expected = {f"n{i}": "created" for i in range(48) if i % 3}
    assert {nid: n.input_hash for nid, n in cached.nodes.items()} == expected
    fresh = new_backend()
    assert {nid: n.input_hash for nid, n in fresh.load().nodes.items()} == expected
    assert cached_serial == fresh.serial() == 48 * 2 + 16
    assert new_backend().fsck().healthy
