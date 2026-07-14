"""Aliases: renaming a resource maps to its state node instead of destroy+create."""

from __future__ import annotations

from pathlib import Path

from atlantide.engine import Engine
from atlantide.providers import local
from atlantide.providers.local import LocalProvider
from atlantide.reconcile import Action
from atlantide.state.sqlite_backend import SqliteStateBackend
from tests.conftest import make_engine


def _engine(tmp: Path) -> Engine:
    return make_engine(local.TYPES, LocalProvider(), backend=None)  # memory backend


def _config(tmp: Path, *, name: str = "a", alias: str | None = None) -> str:
    lifecycle = f", lifecycle=Lifecycle(aliases=({alias!r},))" if alias else ""
    # b depends on the (possibly renamed) node's checksum -> a real edge.
    return (
        "from atlantide.core import Lifecycle\n"
        "from atlantide.providers.local import File\n"
        f"a = File({name!r}, path={str(tmp / 'a.txt')!r}, content='alpha'{lifecycle})\n"
        f"File('b', path={str(tmp / 'b.txt')!r}, content=a.checksum)\n"
    )


async def test_rename_is_noop_not_replace(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    (await engine.apply(_config(tmp_path, name="a"))).unwrap()
    assert set(engine.backend.load().nodes) == {
        "default:local.File:a",
        "default:local.File:b",
    }

    renamed = _config(tmp_path, name="a2", alias="a")

    # plan: the rename + its dependent are NOOP, never DELETE/CREATE.
    planned = engine.plan(renamed).unwrap()
    actions = {c.node_id: c.action for c in planned.changeset}
    assert actions == {
        "default:local.File:a2": Action.NOOP,
        "default:local.File:b": Action.NOOP,
    }

    # apply: state row is rekeyed a -> a2; the old id is gone; b untouched.
    report = (await engine.apply(renamed)).unwrap()
    assert not report.created and not report.deleted
    assert set(engine.backend.load().nodes) == {
        "default:local.File:a2",
        "default:local.File:b",
    }

    # idempotent: re-applying the renamed config is all NOOP (alias now inert).
    report2 = (await engine.apply(renamed)).unwrap()
    assert len(report2.noop) == 2


async def test_rename_without_alias_is_destroy_create(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    (await engine.apply(_config(tmp_path, name="a"))).unwrap()

    planned = engine.plan(_config(tmp_path, name="a2")).unwrap()
    actions = {c.node_id: c.action for c in planned.changeset}
    assert actions["default:local.File:a"] is Action.DELETE
    assert actions["default:local.File:a2"] is Action.CREATE


async def test_rename_persists_across_sqlite_reopen(tmp_path: Path) -> None:
    db = str(tmp_path / "state.db")
    first = make_engine(local.TYPES, LocalProvider(), backend=SqliteStateBackend(db))
    (await first.apply(_config(tmp_path, name="a"))).unwrap()
    first.backend.close()

    reopened = make_engine(local.TYPES, LocalProvider(), backend=SqliteStateBackend(db))
    (await reopened.apply(_config(tmp_path, name="a2", alias="a"))).unwrap()
    assert set(reopened.backend.load().nodes) == {
        "default:local.File:a2",
        "default:local.File:b",
    }
    reopened.backend.close()


async def test_alias_accepts_full_node_id(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    (await engine.apply(_config(tmp_path, name="a"))).unwrap()
    renamed = _config(tmp_path, name="a2", alias="default:local.File:a")  # full old id
    planned = engine.plan(renamed).unwrap()
    assert {c.action for c in planned.changeset} == {Action.NOOP}


async def test_rename_plus_real_change_still_updates(tmp_path: Path) -> None:
    """A rename must not mask a genuine edit to the same resource."""
    engine = _engine(tmp_path)
    (await engine.apply(_config(tmp_path, name="a"))).unwrap()

    # a2 aliases a, but also changes content -> UPDATE, not a masked NOOP.
    renamed_changed = (
        "from atlantide.core import Lifecycle\n"
        "from atlantide.providers.local import File\n"
        f"a = File('a2', path={str(tmp_path / 'a.txt')!r}, content='beta',"
        " lifecycle=Lifecycle(aliases=('a',)))\n"
        f"File('b', path={str(tmp_path / 'b.txt')!r}, content=a.checksum)\n"
    )
    actions = {c.node_id: c.action for c in engine.plan(renamed_changed).unwrap().changeset}
    assert actions["default:local.File:a2"] is Action.UPDATE
    assert "default:local.File:a" not in actions  # old id mapped away, not deleted
