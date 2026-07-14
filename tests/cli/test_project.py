"""``atlantide.toml`` project-defaults loader."""

from __future__ import annotations

from pathlib import Path

from atlantide.cli.project import ComponentSource, ProjectConfig, load_project


def test_missing_file_is_empty_config(tmp_path: Path) -> None:
    assert load_project(tmp_path) == ProjectConfig()


def test_reads_config_and_state(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text('config = "infra.py"\nstate = "infra.db"\n')
    loaded = load_project(tmp_path)
    assert loaded == ProjectConfig(config="infra.py", state="infra.db")


def test_non_string_values_ignored(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text("config = 5\nstate = true\n")
    loaded = load_project(tmp_path)
    assert loaded == ProjectConfig()


def test_reads_secrets_paths(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(
        'state = "s.db"\nsecrets_key = "k.key"\nsecrets_store = "v.enc"\n'
    )
    loaded = load_project(tmp_path)
    assert loaded == ProjectConfig(state="s.db", secrets_key="k.key", secrets_store="v.enc")


def test_reads_component_sources(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(
        "[components.acme]\n"
        'git = "https://github.com/acme/secure-bucket"\n'
        'ref = "v1.2.0"\n'
        'subdir = "src"\n'
        "[components.bare]\n"
        'git = "https://github.com/x/bare"\n'
    )
    loaded = load_project(tmp_path)
    assert loaded.components == {
        "acme": ComponentSource(
            git="https://github.com/acme/secure-bucket", ref="v1.2.0", subdir="src"
        ),
        "bare": ComponentSource(git="https://github.com/x/bare"),
    }


def test_component_without_git_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text("[components.nope]\nref = \"v1\"\n")
    assert load_project(tmp_path).components == {}
