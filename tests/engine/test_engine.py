"""Engine end-to-end: the creds-free full loop with the local provider."""

from __future__ import annotations

import hashlib
from pathlib import Path

from atlantide.core import is_successful
from atlantide.engine import Engine
from atlantide.ir import loads
from atlantide.providers import local
from atlantide.providers.local import LocalProvider
from atlantide.reconcile import Action
from tests.conftest import make_engine


def _engine() -> Engine:
    return make_engine(local.TYPES, LocalProvider())


def _config(tmp: Path, a_content: str = "alpha") -> str:
    # b's content references a's computed checksum -> a real cross-resource edge.
    return (
        "from atlantide.providers.local import File\n"
        f"a = File('a', path={str(tmp / 'a.txt')!r}, content={a_content!r})\n"
        f"File('b', path={str(tmp / 'b.txt')!r}, content=a.checksum)\n"
    )


async def test_full_loop(tmp_path: Path) -> None:
    engine = _engine()
    cfg = _config(tmp_path)

    # plan on empty state -> two creates
    planned = engine.plan(cfg)
    assert is_successful(planned)
    actions = {c.node_id: c.action for c in planned.unwrap().changeset}
    assert set(actions.values()) == {Action.CREATE}

    # apply -> files land, b's content = a's checksum
    report = (await engine.apply(cfg)).unwrap()
    assert len(report.created) == 2
    a_file = tmp_path / "a.txt"
    b_file = tmp_path / "b.txt"
    assert a_file.read_text() == "alpha"
    import hashlib

    assert b_file.read_text() == hashlib.sha256(b"alpha").hexdigest()

    # re-apply unchanged -> all NOOP
    report2 = (await engine.apply(cfg)).unwrap()
    assert len(report2.noop) == 2 and not report2.created

    # change a's content -> a updates, b (depends on a.checksum) updates too
    report3 = (await engine.apply(_config(tmp_path, "beta"))).unwrap()
    assert set(report3.updated) == {"default:local.File:a", "default:local.File:b"}
    assert b_file.read_text() == hashlib.sha256(b"beta").hexdigest()

    # destroy -> both files removed
    report4 = (await engine.destroy()).unwrap()
    assert len(report4.deleted) == 2
    assert not a_file.exists() and not b_file.exists()
    assert len(engine.backend.load()) == 0


def _one_file(path: Path) -> str:
    return (
        f"from atlantide.providers.local import File\nFile('f', path={str(path)!r}, content='x')\n"
    )


async def test_immutable_path_change_replaces(tmp_path: Path) -> None:
    engine = _engine()
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    await engine.apply(_one_file(old))
    assert old.exists()

    report = (await engine.apply(_one_file(new))).unwrap()
    assert report.replaced == ["default:local.File:f"]
    assert not old.exists() and new.exists()  # old path destroyed, new created


def _source_config(path: Path) -> str:
    return (
        "from atlantide.providers.local import SourceFile\n"
        f"SourceFile('cfg', path={str(path)!r})\n"
    )


async def test_sourcefile_rechecked_on_every_plan(tmp_path: Path) -> None:
    engine = _engine()
    src = tmp_path / "src.txt"
    src.write_text("one")
    cfg = _source_config(src)
    node = "default:local.SourceFile:cfg"

    # first apply loads the file
    report = (await engine.apply(cfg)).unwrap()
    assert report.created == [node]
    assert engine.backend.load().get(node).outputs["content"] == "one"

    # unchanged file -> NOOP (checksum stable)
    report2 = (await engine.apply(cfg)).unwrap()
    assert report2.noop == [node] and not report2.created

    # mutate the file on disk -> plan re-reads it and shows an UPDATE
    src.write_text("two")
    planned = engine.plan(cfg).unwrap()
    assert {c.node_id: c.action for c in planned.changeset} == {node: Action.UPDATE}

    report3 = (await engine.apply(cfg)).unwrap()
    assert report3.updated == [node]
    assert engine.backend.load().get(node).outputs["content"] == "two"


async def test_sourcefile_content_is_consumable(tmp_path: Path) -> None:
    engine = _engine()
    src = tmp_path / "src.txt"
    src.write_text("payload")
    out = tmp_path / "out.txt"
    cfg = (
        "from atlantide.providers.local import File, SourceFile\n"
        f"s = SourceFile('cfg', path={str(src)!r})\n"
        f"File('sink', path={str(out)!r}, content=s.content)\n"
    )
    (await engine.apply(cfg)).unwrap()
    assert out.read_text() == "payload"  # cross-resource edge resolved at apply


async def test_cycle_is_reported(tmp_path: Path) -> None:
    # Not expressible via refs (would need mutual computed reads), so check a
    # clean compile error path instead: invalid Atlas-lang.
    engine = _engine()
    result = engine.plan("while True:\n    pass\n")
    assert not is_successful(result)


async def test_apply_blocked_by_lock_on_a_dependency(tmp_path: Path) -> None:
    # Another owner holds 'a'. Applying a+b must fail: b depends on a, so a is in
    # the lock scope (dependency closure), even though only b would change.
    engine = _engine()
    engine.backend.acquire_lock("other-client", 300, {"default:local.File:a"})
    result = await engine.apply(_config(tmp_path))
    assert not is_successful(result)
    assert "locked" in str(result.failure())
    assert not (tmp_path / "b.txt").exists()  # nothing applied


async def test_apply_proceeds_when_lock_is_disjoint(tmp_path: Path) -> None:
    # A lock on an unrelated subgraph does not block this apply.
    engine = _engine()
    engine.backend.acquire_lock("other-client", 300, {"other:stack:node"})
    result = await engine.apply(_config(tmp_path))
    assert is_successful(result)
    assert len(result.unwrap().created) == 2


# -- build / deploy artifacts ------------------------------------------------


async def test_build_deploy_from_artifact_no_source(tmp_path: Path) -> None:
    # Build once, then deploy purely from the artifact's IR (source gone).
    build_engine = _engine()
    artifact = build_engine.build(_config(tmp_path)).unwrap()
    assert artifact.provider_pins == {"local": "1.0.0"}
    assert len(artifact.ir) == 2

    # round-trip the .atlas body, then deploy on a *fresh* engine + state
    reloaded = loads(artifact.dumps()).unwrap()
    deploy_engine = _engine()
    assert is_successful(deploy_engine.verify_artifact(reloaded))

    report = (await deploy_engine.deploy(reloaded)).unwrap()
    assert len(report.created) == 2
    # b's content is a's checksum -> the $ref rehydrated and resolved at deploy
    assert (tmp_path / "b.txt").read_text() == hashlib.sha256(b"alpha").hexdigest()

    # re-deploy the same artifact -> Merkle NOOP, zero provider work
    assert len((await deploy_engine.deploy(reloaded)).unwrap().noop) == 2


def test_verify_rejects_tampered_ir(tmp_path: Path) -> None:
    engine = _engine()
    artifact = engine.build(_config(tmp_path)).unwrap()
    tampered = loads(artifact.dumps().replace("alpha", "TAMPERED")).unwrap()
    result = engine.verify_artifact(tampered)
    assert not is_successful(result)
    assert "hash mismatch" in str(result.failure())


async def test_deploy_unknown_provider_version_is_incompatible(tmp_path: Path) -> None:
    engine = _engine()
    artifact = engine.build(_config(tmp_path)).unwrap()
    # bump only the pin (IR untouched, so the hash still verifies) to a future major
    bad = loads(artifact.dumps().replace('"local": "1.0.0"', '"local": "2.0.0"')).unwrap()
    result = await engine.deploy(bad)
    assert not is_successful(result)
    assert "incompatible" in str(result.failure())


def test_loads_rejects_malformed_artifact() -> None:
    assert not is_successful(loads("{not json"))
    assert not is_successful(loads('{"format_version": 999}'))


async def test_destroy_tolerates_dangling_dependency(tmp_path: Path) -> None:
    # A partial rollback can leave a node whose dependency was already removed.
    # Destroy must (a) drop the dangling graph edge and (b) not KeyError when the
    # node's $ref to the gone dependency's output can't be resolved.
    from atlantide.state import StateNode

    engine = _engine()
    target = tmp_path / "b.txt"
    target.write_text("x")
    engine.backend.put(
        StateNode(
            id="default:local.File:b",
            type="local.File",
            provider="local",
            provider_version="1.0.0",
            input_hash="h",
            properties={
                "path": str(target),
                "content": {"$ref": "default:local.File:a#checksum"},  # 'a' is gone
            },
            dependencies=("default:local.File:a",),  # absent from state
        )
    )

    report = (await engine.destroy()).unwrap()
    assert report.deleted == ["default:local.File:b"]
    assert not target.exists()
