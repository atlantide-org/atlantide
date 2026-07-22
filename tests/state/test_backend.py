"""State backend behaviour — identical across every backend (modularity).

Parametrized over memory, sqlite, s3 and postgres by ``make_backend``; a backend
that cannot pass this file unchanged is not a drop-in replacement.
"""

from __future__ import annotations

from atlantide.core import is_successful

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
