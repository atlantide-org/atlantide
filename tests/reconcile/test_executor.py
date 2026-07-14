"""Executor: apply create/update/replace/delete, Merkle-skip, resume, guards."""

from __future__ import annotations

import pytest

from atlantide.core import PreventDestroyError, is_successful
from atlantide.state import MemoryStateBackend, SqliteStateBackend

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"


def _backends(tmp_path: object) -> list[object]:
    return [MemoryStateBackend(), SqliteStateBackend(str(tmp_path / "s.db"))]  # type: ignore[operator]


def test_create_persists_and_resolves_refs(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        report = h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n")
        assert sorted(report.created) == [A, B]
        state = backend.load()  # type: ignore[attr-defined]
        assert set(state.nodes) == {A, B}
        # b's ref resolved to a's create output "a:1"
        assert h.fake().created_ref("b") == "a:1"
        assert state.get(B).outputs == {"out": "b:2"}


def test_second_apply_is_noop_zero_provider_calls(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n")
        h.fake().reset()
        report = h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n")
        assert sorted(report.noop) == [A, B]
        assert report.created == []
        assert h.fake().calls == []  # Merkle skip: provider never touched


def test_mutable_update(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        h.apply("Box('a', size=1, label='x')\n")
        h.fake().reset()
        report = h.apply("Box('a', size=1, label='y')\n")
        assert report.updated == [A]
        assert h.fake().calls == [("update", "a")]


def test_immutable_replace_destroys_then_creates(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        h.apply("Box('a', size=1)\n")
        h.fake().reset()
        report = h.apply("Box('a', size=2)\n")
        assert report.replaced == [A]
        assert h.fake().calls == [("delete", "a"), ("create", "a")]
        assert backend.load().get(A).outputs == {"out": "a:2"}  # type: ignore[attr-defined]


def test_delete_removed_resource(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        h.apply("Box('a', size=1)\nBox('b', size=2)\n")
        h.fake().reset()
        report = h.apply("Box('a', size=1)\n")
        assert report.deleted == [B]
        assert set(backend.load().nodes) == {A}  # type: ignore[attr-defined]
        assert h.fake().calls == [("delete", "b")]


def test_prevent_destroy_blocks_plan(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        h.apply("Box('a', size=1, lifecycle=Lifecycle(prevent_destroy=True))\n")
        result = h.plan_only("")  # remove everything
        assert not is_successful(result)
        assert isinstance(result.failure(), PreventDestroyError)


def test_rollback_undoes_completed_creates(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        h.fake().fail_create.add("b")  # dependent fails after 'a' created
        with pytest.raises(ExceptionGroup):
            h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n", on_failure="rollback")
        # saga: 'a' created then compensated (deleted). 'b's failed create leaves a
        # write-ahead 'creating' row so the (possibly leaked) resource is reclaimable.
        assert h.fake().calls == [("create", "a"), ("create", "b"), ("delete", "a")]
        nodes = backend.load().nodes  # type: ignore[attr-defined]
        assert set(nodes) == {B}
        assert nodes[B].status == "creating"


def test_rollback_delete_carries_created_output(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        h.fake().fail_create.add("b")  # dependent fails after 'a' created
        with pytest.raises(ExceptionGroup):
            h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n", on_failure="rollback")
        # the compensating delete of 'a' gets 'a's real created output, not an
        # unresolved Ref -> a provider that locates by id can act on the resource
        # actually created (rather than re-discovering it by shared attributes).
        assert h.fake().deleted_output("a") == "a:1"


def test_rollback_restores_prior_on_failed_update(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        # last-good baseline; 'c' depends on 'a' so 'a' applies first
        h.apply("a = Box('a', size=1, label='x')\nBox('c', size=9, ref=a.out)\n")
        h.fake().reset()
        h.fake().fail_update.add("c")  # 'c' update fails after 'a' update commits
        with pytest.raises(ExceptionGroup):
            h.apply(
                "a = Box('a', size=1, label='y')\nBox('c', size=9, ref=a.out, label='z')\n",
                on_failure="rollback",
            )
        # 'a' updated then rolled back to its prior inputs; state row restored
        assert ("update", "a") in h.fake().calls
        assert h.fake().calls.count(("update", "a")) == 2  # forward + compensating
        assert backend.load().get(A).outputs == {"out": "a:1"}  # type: ignore[attr-defined]


def test_halt_on_failure_then_resume(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        h.fake().fail_create.add("b")  # dependent fails mid-apply
        with pytest.raises(ExceptionGroup):
            h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n", on_failure="halt")
        # incremental persist: 'a' committed (created); 'b' left write-ahead 'creating'
        nodes = backend.load().nodes  # type: ignore[attr-defined]
        assert set(nodes) == {A, B}
        assert nodes[A].status == "created"
        assert nodes[B].status == "creating"

        # fix and resume: 'a' is a Merkle NOOP, 'b's creating row is re-created
        h.fake().fail_create.clear()
        h.fake().reset()
        report = h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n", on_failure="halt")
        assert report.noop == [A]
        assert report.created == [B]
        assert h.fake().calls == [("create", "b")]
        assert set(backend.load().nodes) == {A, B}  # type: ignore[attr-defined]


def test_write_ahead_creating_row_reclaimed_by_destroy(tmp_path: object) -> None:
    for backend in _backends(tmp_path):
        h = Harness(backend)  # type: ignore[arg-type]
        h.fake().fail_create.add("a")  # create fails -> only the write-ahead row lands
        with pytest.raises(ExceptionGroup):
            h.apply("Box('a', size=1)\n", on_failure="halt")
        nodes = backend.load().nodes  # type: ignore[attr-defined]
        assert set(nodes) == {A}
        assert nodes[A].status == "creating"  # tracked despite the failed create

        # destroy sees the creating row and reclaims it (delete is idempotent).
        h.fake().fail_create.clear()
        h.fake().reset()
        report = h.apply("")  # empty config -> DELETE the tracked node
        assert report.deleted == [A]
        assert h.fake().calls == [("delete", "a")]
        assert set(backend.load().nodes) == set()  # type: ignore[attr-defined]
