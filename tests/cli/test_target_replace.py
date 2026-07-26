"""``--target`` and ``--replace``: acting on part of a graph, and forcing a rebuild.

These are the escape hatches for when one resource is wrong and the alternatives
are editing the config and hoping, or destroying everything. The risk they carry
is the mirror of their usefulness — a plan narrowed to nothing reads exactly like
a plan with nothing to do — so most of what is asserted here is that the
narrowing is *visible* and that it cannot corrupt a later full run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantide.state import SqliteStateBackend
from tests.support import Cli

cli = Cli()

A = "default:local.File:a"
B = "default:local.File:b"
C = "default:local.File:c"


def _config(tmp_path: Path, content: str = "one") -> Path:
    """Three files; `b` depends on `a` by reading its checksum, `c` is independent."""
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"a = File('a', path={str(tmp_path / 'a.txt')!r}, content={content!r})\n"
        f"b = File('b', path={str(tmp_path / 'b.txt')!r}, content=a.checksum)\n"
        f"c = File('c', path={str(tmp_path / 'c.txt')!r}, content='independent')\n"
    )
    return cfg


def _applied(tmp_path: Path) -> tuple[Path, Path]:
    cfg = _config(tmp_path)
    state = tmp_path / "state.db"
    cli.run("apply", cfg, "--state", state, "-y")
    return cfg, state


def _hashes(state: Path) -> dict[str, str]:
    backend = SqliteStateBackend(str(state))
    try:
        return {nid: node.input_hash for nid, node in backend.load().nodes.items()}
    finally:
        backend.close()


# -- the invariant that makes targeting safe ----------------------------------


def test_a_targeted_apply_leaves_untargeted_hashes_untouched(tmp_path: Path) -> None:
    """The reason `--target` filters the changeset rather than the IR.

    Lowering a subset would change each remaining node's Merkle hash — the hash
    folds in its dependencies' — and persisting those would make the next *full*
    run report changes on resources nobody touched. A NOOP writes nothing, so the
    stored hashes have to come through a targeted apply bit-for-bit.
    """
    _cfg, state = _applied(tmp_path)
    before = _hashes(state)

    changed = _config(tmp_path, content="two")
    cli.run("apply", changed, "--state", state, "-t", "local.File:c", "-y")

    after = _hashes(state)
    assert after[A] == before[A], "an untargeted node's hash was rewritten"
    assert after[B] == before[B]


def test_a_full_run_after_a_targeted_one_still_sees_the_pending_work(tmp_path: Path) -> None:
    """The consequence of the above, from the operator's side: work skipped by a
    targeted apply must still be waiting afterwards, not silently marked done."""
    _cfg, state = _applied(tmp_path)
    changed = _config(tmp_path, content="two")
    cli.ok("apply", changed, "--state", state, "-t", "local.File:c", "-y")

    result = cli.run("plan", changed, "--state", state)
    assert "local.File:a" in result.output
    assert "1 to change" in result.output or "2 to change" in result.output


# -- the closure --------------------------------------------------------------


def test_targeting_a_dependent_pulls_in_what_it_needs(tmp_path: Path) -> None:
    """`b` reads `a`'s checksum, so `b` cannot be built without `a`."""
    cfg = _config(tmp_path)
    state = tmp_path / "state.db"

    cli.run("apply", cfg, "--state", state, "-t", "local.File:b", "-y")

    assert (tmp_path / "a.txt").exists(), "the dependency was pulled in"
    assert (tmp_path / "b.txt").exists()
    assert not (tmp_path / "c.txt").exists(), "the unrelated resource was left alone"


def test_targeting_a_dependency_does_not_pull_in_its_dependents(tmp_path: Path) -> None:
    """The closure runs one way for an apply: `a` is buildable without `b`."""
    cfg = _config(tmp_path)
    state = tmp_path / "state.db"

    cli.ok("apply", cfg, "--state", state, "-t", "local.File:a", "-y")

    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()


def test_a_destroy_closes_over_dependents_instead(tmp_path: Path) -> None:
    """The other direction: removing `a` means removing what still points at it.
    Closing over dependencies here would destroy what `a` is built from and leave
    `a` dangling — the opposite of what was asked."""
    _, state = _applied(tmp_path)

    cli.run("destroy", "--state", state, "-t", "local.File:a", "-y")

    assert not (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists(), "the dependent went too"
    assert (tmp_path / "c.txt").exists(), "the unrelated resource survived"


# -- visibility ---------------------------------------------------------------


def test_a_targeted_plan_says_it_is_a_subset(tmp_path: Path) -> None:
    """A plan narrowed to nothing reads exactly like a plan with nothing to do."""
    _cfg, state = _applied(tmp_path)
    changed = _config(tmp_path, content="two")

    result = cli.run("plan", changed, "--state", state, "-t", "local.File:c")
    assert "targeting" in result.output
    assert "will not change" in result.output


def test_an_untargeted_plan_says_nothing_extra(tmp_path: Path) -> None:
    cfg, state = _applied(tmp_path)
    result = cli.run("plan", cfg, "--state", state)
    assert "targeting" not in result.output


def test_a_targeted_destroy_previews_only_what_goes(tmp_path: Path) -> None:
    """The preview is what gets approved, so it has to be the selection."""
    _, state = _applied(tmp_path)
    result = cli.run("destroy", "--state", state, "-t", "local.File:c", input="n\n")
    assert "local.File:c" in result.output
    assert "local.File:a" not in result.output
    assert "targeting 1 of 3" in result.output


# -- selecting ----------------------------------------------------------------


def test_a_pattern_matching_nothing_is_an_error(tmp_path: Path) -> None:
    """Silent no-op targeting is how someone concludes a resource is fine."""
    cfg, state = _applied(tmp_path)
    result = cli.run("plan", cfg, "--state", state, "-t", "local.File:ghost")
    assert result.exit_code == 1
    assert "matched no resource" in result.output


def test_a_glob_selects_several(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = tmp_path / "state.db"
    cli.run("apply", cfg, "--state", state, "-t", "local.File:*", "-y")
    assert (tmp_path / "c.txt").exists()


def test_a_full_node_id_works(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = tmp_path / "state.db"
    cli.run("apply", cfg, "--state", state, "-t", C, "-y")
    assert (tmp_path / "c.txt").exists()


# -- replace ------------------------------------------------------------------


def test_replace_recreates_a_resource_config_says_is_fine(tmp_path: Path) -> None:
    """The escape hatch: the resource is wrong in a way the config cannot see, so
    the diff has nothing to report and a plain apply is a no-op."""
    cfg, state = _applied(tmp_path)

    result = cli.run("plan", cfg, "--state", state, "--replace", "local.File:c")
    assert "replace" in result.output
    assert "local.File:c" in result.output


def test_replace_actually_rebuilds_it(tmp_path: Path) -> None:
    cfg, state = _applied(tmp_path)
    (tmp_path / "c.txt").unlink()  # corrupted out of band

    cli.run("apply", cfg, "--state", state, "--replace", "local.File:c", "-y")
    assert (tmp_path / "c.txt").read_text() == "independent"


def test_replace_still_respects_prevent_destroy(tmp_path: Path) -> None:
    """`--replace` destroys and recreates, so it has to meet the one guard that
    exists to stop an unintended destroy — otherwise the flag is a way around it.
    """
    cfg = tmp_path / "protected.py"
    cfg.write_text(
        "from atlantide.core import Lifecycle\n"
        "from atlantide.providers.local import File\n"
        f"File('p', path={str(tmp_path / 'p.txt')!r}, content='x',\n"
        "     lifecycle=Lifecycle(prevent_destroy=True))\n"
    )
    state = tmp_path / "state.db"
    assert cli.run("apply", cfg, "--state", state, "-y").exit_code == 0

    result = cli.run("plan", cfg, "--state", state, "--replace", "local.File:p")
    assert result.exit_code == 1
    assert "prevent_destroy" in result.output


def test_replace_composes_with_target(tmp_path: Path) -> None:
    cfg, state = _applied(tmp_path)
    result = cli.run(
        "plan",
        cfg,
        "--state",
        state,
        "-t",
        "local.File:c",
        "--replace",
        "local.File:c",
    )
    assert "replace" in result.output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
