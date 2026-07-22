"""``atlantide.toml`` project-defaults loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantide.cli.project import (
    ComponentSource,
    ProjectConfig,
    ProjectError,
    SecretsConfig,
    StateConfig,
    load_project,
)


def test_missing_file_is_empty_config(tmp_path: Path) -> None:
    assert load_project(tmp_path) == ProjectConfig()


def test_reads_config_and_state(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text('config = "infra.py"\nstate = "infra.db"\n')
    loaded = load_project(tmp_path)
    assert loaded == ProjectConfig(root=tmp_path, config="infra.py", state="infra.db")


def test_non_string_values_ignored(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text("config = 5\nstate = true\n")
    loaded = load_project(tmp_path)
    assert loaded == ProjectConfig(root=tmp_path)


def test_reads_secrets_paths(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(
        'state = "s.db"\nsecrets_key = "k.key"\nsecrets_store = "v.enc"\n'
    )
    loaded = load_project(tmp_path)
    assert loaded == ProjectConfig(
        root=tmp_path, state="s.db", secrets_key="k.key", secrets_store="v.enc"
    )


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


def test_defaults_to_local_state_and_keyfile_secrets(tmp_path: Path) -> None:
    assert load_project(tmp_path).state_backend == StateConfig(backend="local")
    assert load_project(tmp_path).secrets == SecretsConfig(provider="keyfile")


def test_reads_s3_state_backend(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(
        "[state]\n"
        'backend    = "s3"\n'
        'bucket     = "acme-state"\n'
        'key        = "prod/atlantide.json"\n'
        'lock_table = "atlantide-locks"\n'
        'kms_key_id = "alias/atlantide"\n'
        'region     = "eu-north-1"\n'
    )
    loaded = load_project(tmp_path)
    assert loaded.state_backend == StateConfig(
        backend="s3",
        bucket="acme-state",
        key="prod/atlantide.json",
        lock_table="atlantide-locks",
        kms_key_id="alias/atlantide",
        region="eu-north-1",
    )
    assert loaded.state_backend.is_remote


def test_reads_postgres_state_backend(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(
        '[state]\nbackend = "postgres"\ndsn = "postgresql://db/atlantide"\nschema = "infra"\n'
    )
    assert load_project(tmp_path).state_backend == StateConfig(
        backend="postgres", dsn="postgresql://db/atlantide", schema="infra"
    )


def test_reads_ssm_secrets_provider(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(
        '[secrets]\nprovider = "ssm"\nprefix = "/atlantide/prod/"\nregion = "eu-north-1"\n'
    )
    assert load_project(tmp_path).secrets == SecretsConfig(
        provider="ssm", prefix="/atlantide/prod/", region="eu-north-1"
    )


def test_found_in_a_parent_directory(tmp_path: Path) -> None:
    """Running from a subdirectory must not silently drop the whole project file."""
    (tmp_path / "atlantide.toml").write_text('config = "infra.py"\n')
    nested = tmp_path / "stacks" / "prod"
    nested.mkdir(parents=True)
    loaded = load_project(nested)
    assert loaded.config == "infra.py"
    assert loaded.root == tmp_path


def test_relative_paths_anchor_to_the_project_root(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text('state = "atlantide.db"\n')
    nested = tmp_path / "sub"
    nested.mkdir()
    loaded = load_project(nested)
    assert loaded.resolve(loaded.state or "") == tmp_path / "atlantide.db"
    assert loaded.resolve("/abs/path") == Path("/abs/path")


def test_profile_overlays_the_top_level(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(
        'config = "infra.py"\nparallelism = 2\n'
        '[state]\nbackend = "local"\n'
        "[profile.prod]\nparallelism = 16\n"
        '[profile.prod.state]\nbackend = "s3"\nbucket = "acme"\n'
        'key = "prod/atlantide.json"\nlock_table = "locks"\n'
    )
    base = load_project(tmp_path)
    assert base.parallelism == 2 and not base.state_backend.is_remote

    prod = load_project(tmp_path, profile="prod")
    assert prod.profile == "prod"
    assert prod.parallelism == 16
    assert prod.state_backend.backend == "s3"
    assert prod.state_backend.bucket == "acme"
    assert prod.config == "infra.py"  # untouched keys survive the overlay


def test_profile_tables_are_not_read_as_config(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(
        '[profile.prod.state]\nbackend = "s3"\nbucket = "acme"\n'
    )
    assert load_project(tmp_path).state_backend.backend == "local"


def test_unknown_profile_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text("[profile.prod]\nparallelism = 4\n")
    with pytest.raises(ProjectError, match=r"no \[profile\.staging\].*defined: prod"):
        load_project(tmp_path, profile="staging")
