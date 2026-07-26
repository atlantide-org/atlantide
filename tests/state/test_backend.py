"""State backend behaviour — identical across every backend (modularity).

Parametrized over memory, sqlite, s3 and postgres by ``make_backend``; a backend
that cannot pass this file unchanged is not a drop-in replacement.
"""

from __future__ import annotations

import pytest

from atlantide.core import is_successful
from atlantide.core.errors import FencedWriteError

from .conftest import BackendFactory, FakeClock, node


def test_put_load_roundtrip(make_backend: BackendFactory) -> None:
    backend = make_backend()
    written = node("a", dependencies=("x",), outputs={"arn": "arn::a", "n": 3})
    backend.put(written)
    loaded = backend.load()
    assert len(loaded) == 1
    assert loaded.get("a") == written


def test_upsert_overwrites(make_backend: BackendFactory) -> None:
    backend = make_backend()
    backend.put(node("a", input_hash="h1"))
    backend.put(node("a", input_hash="h2"))
    assert backend.load().get("a").input_hash == "h2"
    assert len(backend.load()) == 1


def test_delete(make_backend: BackendFactory) -> None:
    backend = make_backend()
    backend.put(node("a"))
    backend.put(node("b"))
    backend.delete("a")
    graph = backend.load()
    assert "a" not in graph and "b" in graph
    backend.delete("missing")  # no-op, no error


def test_serial_bumps_on_mutation(make_backend: BackendFactory) -> None:
    """The serial advances when stored state changes, and never goes backwards.

    Deliberately an inequality: a backend may skip a write whose node is already
    stored verbatim (the s3 one does, since every write there rewrites the whole
    document), and skipping a write that changes nothing is not a mutation.
    """
    backend = make_backend()
    assert backend.serial() == 0
    backend.put(node("a"))
    first = backend.serial()
    assert first == 1
    backend.put(node("a"))
    assert backend.serial() >= first
    backend.put(node("a", input_hash="changed"))
    changed = backend.serial()
    assert changed > first
    backend.delete("a")
    assert backend.serial() > changed
    deleted = backend.serial()
    backend.delete("a")  # no row deleted -> no bump
    assert backend.serial() == deleted


def test_outputs_merge_later_applies_win(make_backend: BackendFactory) -> None:
    backend = make_backend()
    assert backend.outputs() == {}
    backend.set_outputs({"dev:a": 1, "dev:b": 2})
    backend.set_outputs({"dev:b": 3, "prod:c": 4})  # later apply overwrites 'dev:b'
    assert backend.outputs() == {"dev:a": 1, "dev:b": 3, "prod:c": 4}


# -- fine-grained (subgraph) locking -----------------------------------------


def test_disjoint_scopes_dont_conflict(make_backend: BackendFactory) -> None:
    backend = make_backend(clock=FakeClock())
    assert is_successful(backend.acquire_lock("alice", 30, {"a", "b"}))
    # bob locks a disjoint subgraph -> both hold concurrently
    assert is_successful(backend.acquire_lock("bob", 30, {"c", "d"}))


def test_overlapping_scope_conflicts(make_backend: BackendFactory) -> None:
    backend = make_backend(clock=FakeClock())
    backend.acquire_lock("alice", 30, {"a", "b"})
    contended = backend.acquire_lock("bob", 30, {"b", "c"})  # 'b' overlaps
    assert not is_successful(contended)
    err = str(contended.failure())
    assert "alice" in err and "'b'" in err


def test_lock_is_reentrant_for_same_owner(make_backend: BackendFactory) -> None:
    backend = make_backend(clock=FakeClock())
    assert is_successful(backend.acquire_lock("alice", 30, {"a", "b"}))
    # same owner may re-lock an overlapping (or growing) scope
    assert is_successful(backend.acquire_lock("alice", 30, {"b", "c"}))


def test_empty_scope_is_noop_success(make_backend: BackendFactory) -> None:
    backend = make_backend(clock=FakeClock())
    assert is_successful(backend.acquire_lock("alice", 30, set()))
    # locks nothing, so anyone can still take real nodes
    assert is_successful(backend.acquire_lock("bob", 30, {"a"}))


def test_expired_hold_is_reclaimable(make_backend: BackendFactory) -> None:
    clock = FakeClock()
    backend = make_backend(clock=clock)
    backend.acquire_lock("alice", 10, {"a"})
    clock.advance(11)  # alice's hold on 'a' expired
    reclaimed = backend.acquire_lock("bob", 10, {"a"})
    assert is_successful(reclaimed)
    assert reclaimed.unwrap().owner == "bob"


def test_release_frees_only_owners_holds(make_backend: BackendFactory) -> None:
    backend = make_backend(clock=FakeClock())
    backend.acquire_lock("alice", 30, {"a"})
    backend.acquire_lock("bob", 30, {"b"})
    assert is_successful(backend.release_lock("alice"))
    # 'a' is free again; bob's hold on 'b' is untouched
    assert is_successful(backend.acquire_lock("carol", 30, {"a"}))
    assert not is_successful(backend.acquire_lock("carol", 30, {"b"}))


def test_put_many_stores_every_node(make_backend: BackendFactory) -> None:
    backend = make_backend()
    backend.put_many([node("a"), node("b"), node("c")])
    assert set(backend.load().nodes) == {"a", "b", "c"}
    backend.put_many([])  # no nodes, no error, no change
    assert set(backend.load().nodes) == {"a", "b", "c"}


def test_put_many_upserts(make_backend: BackendFactory) -> None:
    backend = make_backend()
    backend.put(node("a"))
    backend.put_many([node("a", input_hash="h1"), node("b")])
    assert backend.load().nodes["a"].input_hash == "h1"


def test_locks_are_visible_and_breakable(make_backend: BackendFactory) -> None:
    """An operator has to be able to see and break a lease a dead run left behind."""
    backend = make_backend(clock=FakeClock())
    backend.acquire_lock("alice", 30, {"a", "b"})
    held = backend.locks()
    assert set(held) == {"a", "b"}
    assert held["a"].owner == "alice"
    assert held["a"].expires_at == held["b"].expires_at > 0

    assert backend.force_unlock({"a"}) == 1
    assert set(backend.locks()) == {"b"}
    assert is_successful(backend.acquire_lock("bob", 30, {"a"}))
    assert not is_successful(backend.acquire_lock("bob", 30, {"b"}))


def test_force_unlock_of_an_unheld_node_is_a_no_op(make_backend: BackendFactory) -> None:
    backend = make_backend()
    assert backend.force_unlock({"nope"}) == 0


# -- write fencing ------------------------------------------------------------
#
# The authoritative half of the concurrency guarantee. `LeaseGuard` is a local
# clock check and can be wrong; these assert that the *store* refuses a write
# from a run that no longer holds the lock, which is what stands between two
# concurrent applies and a silently merged state.


def test_an_unbound_backend_writes_freely(make_backend: BackendFactory) -> None:
    """Administrative writes — `state restore`, `state migrate` — happen outside
    any run and must not need a lease."""
    backend = make_backend()
    backend.put(node("a"))
    backend.delete("a")
    assert len(backend.load()) == 0


def test_a_write_is_refused_once_the_lock_has_been_taken_away(
    make_backend: BackendFactory,
) -> None:
    """The scenario the fence exists for: run A's lease is broken, run B takes
    the node, and A's next write must not land."""
    backend = make_backend()
    lease = backend.acquire_lock("run-a", 300.0, {"a"}).unwrap()
    backend.bind_lease(lease)
    backend.put(node("a", input_hash="from-a"))  # still legitimate

    backend.force_unlock({"a"})
    assert is_successful(backend.acquire_lock("run-b", 300.0, {"a"}))

    with pytest.raises(FencedWriteError, match="run-b"):
        backend.put(node("a", input_hash="stale-from-a"))
    assert backend.load().get("a").input_hash == "from-a", "run B's view is intact"


def test_a_write_is_refused_after_the_lease_expires_and_is_reclaimed(
    make_backend: BackendFactory,
) -> None:
    """Expiry alone is not enough — the hold has to actually change hands, which
    is what makes the write unsafe rather than merely late."""
    clock = FakeClock()
    backend = make_backend(clock=clock)
    lease = backend.acquire_lock("run-a", 60.0, {"a"}).unwrap()
    backend.bind_lease(lease)

    clock.advance(61.0)
    assert is_successful(backend.acquire_lock("run-b", 60.0, {"a"}))

    with pytest.raises(FencedWriteError):
        backend.put(node("a"))


def test_a_write_outside_the_lease_scope_is_refused(
    make_backend: BackendFactory,
) -> None:
    """Not a race but a bug — and one whose symptom is a node written with
    nothing protecting it, which is exactly what the lock was for."""
    backend = make_backend()
    backend.bind_lease(backend.acquire_lock("run-a", 300.0, {"a"}).unwrap())

    with pytest.raises(FencedWriteError, match="outside this run's lock scope"):
        backend.put(node("b"))


def test_reacquiring_supersedes_the_earlier_lease(make_backend: BackendFactory) -> None:
    """Same owner, newer epoch. A process that re-acquires must not let a
    still-in-flight write from its previous attempt land underneath the new one.
    """
    backend = make_backend()
    first = backend.acquire_lock("run-a", 300.0, {"a"}).unwrap()
    second = backend.acquire_lock("run-a", 300.0, {"a"}).unwrap()
    assert second.fence > first.fence, "each acquisition mints a newer epoch"

    backend.bind_lease(first)
    with pytest.raises(FencedWriteError, match="superseded"):
        backend.put(node("a"))

    backend.bind_lease(second)
    backend.put(node("a"))  # the current lease may write


def test_unbinding_restores_unfenced_writes(make_backend: BackendFactory) -> None:
    """`with_lock` unbinds in its `finally`; a backend left bound would refuse
    every later administrative write in the same process."""
    backend = make_backend()
    backend.bind_lease(backend.acquire_lock("run-a", 300.0, {"a"}).unwrap())
    backend.bind_lease(None)
    backend.put(node("b"))  # outside the old scope, and fine


def test_every_delete_is_fenced_too(make_backend: BackendFactory) -> None:
    """A delete is a write. Fencing only the upserts would leave the destructive
    half of the API unguarded."""
    backend = make_backend()
    backend.put(node("a"))
    backend.bind_lease(backend.acquire_lock("run-a", 300.0, {"a"}).unwrap())
    backend.force_unlock({"a"})
    assert is_successful(backend.acquire_lock("run-b", 300.0, {"a"}))

    with pytest.raises(FencedWriteError):
        backend.delete("a")
    assert "a" in backend.load()


def test_bulk_writes_are_fenced_on_every_node_they_touch(
    make_backend: BackendFactory,
) -> None:
    backend = make_backend()
    backend.bind_lease(backend.acquire_lock("run-a", 300.0, {"a"}).unwrap())

    with pytest.raises(FencedWriteError):
        backend.put_many([node("a"), node("b")])  # `b` is outside the scope
    assert len(backend.load()) == 0, "all of it, or none"
