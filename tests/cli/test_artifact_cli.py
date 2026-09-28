"""``graph``, ``build``, ``verify`` and ``deploy``: the config as a diagram and as an artifact."""

from __future__ import annotations

from pathlib import Path

from tests.cli.conftest import file_config
from tests.support import Cli

cli = Cli()


def test_graph_mermaid_boxes_each_stack(tmp_path: Path) -> None:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.core import Stack\n"
        "from atlantide.providers.local import File\n"
        "for env in ('dev', 'prod'):\n"
        "    with Stack(env, region='us-east-1'):\n"
        "        File('f', path=f'{env}.txt', content='x')\n"
    )
    result = cli.run("graph", cfg, "--format", "mermaid")
    assert 'subgraph cluster0["dev"]' in result.output
    assert 'subgraph cluster1["prod"]' in result.output
    assert result.output.count("subgraph") == 2
    assert result.output.count("end") >= 2
    # node label drops the stack prefix (the box already names it)
    assert '["local.File:f"]' in result.output


def test_build_verify_deploy_roundtrip(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    art = tmp_path / "app.atlas"
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"

    built = cli.ok("build", cfg, "-o", art)
    assert art.exists() and "built" in built.output

    verified = cli.ok("verify", art)
    assert "verified" in verified.output

    # deploy from the artifact alone — no config path passed
    deployed = cli.ok("deploy", art, "--state", state, "-y")
    assert out.read_text() == "hi"
    assert "Applied: 1 to add" in deployed.output


def test_verify_corrupted_artifact_errors(tmp_path: Path) -> None:
    art = tmp_path / "bad.atlas"
    art.write_text("{ not valid json")
    result = cli.run("verify", art)
    assert result.exit_code == 1
    assert "error" in result.output
