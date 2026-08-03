"""What a type checker sees — the whole point of declaring an ``EnvSchema``.

A declared schema exists so an editor can complete ``env.<var>`` and flag a
typo. That benefit lives entirely in annotations, and ``pyproject.toml`` sets
``packages = ["atlantide"]`` — so user configs and ``examples/`` are never
type-checked, and nothing else in the suite would notice if it regressed.

The mechanism is easy to break by accident: ``EnvSchema.__getattr__`` is hidden
behind ``if not TYPE_CHECKING`` so that only *declared* fields type-check, and
``EnvView`` re-declares a visible one so the ``var()`` form stays permissive.
Remove either half and one of the two assertions below fails.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SNIPPET = """
from atlantide.core import Config, EnvSchema, var


class AppEnv(EnvSchema):
    region: str
    price_class: str = "PriceClass_100"
    versioning: bool = False


declared = Config(AppEnv, envs={"dev": {"region": "us-east-1"}})
for env in declared.envs():
    reveal_type(env)
    reveal_type(env.price_class)
    reveal_type(env.versioning)
    env.pirce_class

untyped = Config(schema={"size": var(int, default=1)}, envs={"dev": {}})
for legacy in untyped.envs():
    reveal_type(legacy)
    reveal_type(legacy.anything_at_all)
"""


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> str:
    """mypy's output for the snippet, run against the real package."""
    source: Path = tmp_path_factory.mktemp("typing") / "snippet.py"
    source.write_text(_SNIPPET)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-error-summary",
            "--no-incremental",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    if "Revealed type" not in result.stdout:
        pytest.skip(f"mypy did not run: {result.stdout or result.stderr}")
    return result.stdout


def test_a_declared_environment_is_typed_as_its_own_class(report: str) -> None:
    """`env` is the user's class, so an editor knows what to offer."""
    assert 'Revealed type is "snippet.AppEnv"' in report


def test_declared_fields_carry_their_declared_types(report: str) -> None:
    assert 'Revealed type is "str"' in report
    assert 'Revealed type is "bool"' in report


def test_a_typo_on_a_declared_environment_is_a_type_error(report: str) -> None:
    """The half that a visible `__getattr__` on `EnvSchema` would silently lose."""
    assert 'has no attribute "pirce_class"' in report


def test_the_var_form_stays_permissive(report: str) -> None:
    """`EnvView` re-declares `__getattr__`, so a config that never declared a
    schema class gains no new diagnostics — this is what keeps the shipped
    `var()` API working in a user's editor."""
    assert 'Revealed type is "atlantide.core.config.EnvView"' in report
    assert 'Revealed type is "Any"' in report
    assert "anything_at_all" not in report


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
