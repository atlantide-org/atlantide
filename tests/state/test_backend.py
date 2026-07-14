"""State backend behaviour — identical across memory and sqlite (modularity)."""

from __future__ import annotations

from atlantide.core import is_successful
from atlantide.state import StateNode

from .conftest import BackendFactory, FakeClock


def _node(node_id: str, **kw: object) -> StateNode:
    base = dict(
        id=node_id,
        type="test.T",
        provider="test",
        provider_version="1.0.0",
        input_hash="h0",
        outputs={"arn": f"arn::{node_id}"},
        dependencies=(),
    )
    base.update(kw)
    return StateNode(**base)  # type: ignore[arg-type]


def test_put_load_roundtrip(make_backend: BackendFactory) -> None:
    backend = make_backend()
    node = _node("a", dependencies=("x",), outputs={"arn": "arn::a", "n": 3})
    backend.put(node)
    loaded = backend.load()
    assert len(loaded) == 1
    got = loaded.get("a")
    assert got == node


def test_upsert_overwrites(make_backend: BackendFactory) -> None:
    backend = make_backend()
    backend.put(_node("a", input_hash="h1"))
    backend.put(_node("a", input_hash="h2"))
    assert backend.load().get("a").input_hash == "h2"
    assert len(backend.load()) == 1


def test_delete(make_backend: BackendFactory) -> None:
    backend = make_backend()
    backend.put(_node("a"))
    backend.put(_node("b"))
    backend.delete("a")
    graph = backend.load()
    assert "a" not in graph and "b" in graph
    backend.delete("missing")  # no-op, no error


def test_serial_bumps_on_mutation(make_backend: BackendFactory) -> None:
    backend = make_backend()
    assert backend.serial() == 0
    backend.put(_node("a"))
    assert backend.serial() == 1
    backend.put(_node("a"))
    assert backend.serial() == 2
    backend.delete("a")
    assert backend.serial() == 3
    backend.delete("a")  # no row deleted -> no bump
    assert backend.serial() == 3


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
