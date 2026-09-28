"""``--env`` against a config that reads environments only through ``config.env()``."""

from __future__ import annotations

from typing import Any, ClassVar

from returns.result import Failure

from atlantide.core import Resource, immutable
from atlantide.lang import evaluate_source


class Widget(Resource):
    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable()


SOURCE = (
    "from atlantide.core import Config, Stack\n"
    "config = Config(envs={'dev': {'region': 'r'}, 'prod': {'region': 'r'}})\n"
    "env = config.env('prod')\n"
    "with Stack(env.name, config=env):\n"
    "    Widget('w', size=1)\n"
)


def _run(**kw: Any) -> Any:
    return evaluate_source(SOURCE, extra_globals={"Widget": Widget}, **kw)


def test_nothing_is_excluded_without_a_selection() -> None:
    """Otherwise `prod` is excluded and its new resources plan as unchanged."""
    registry = _run().unwrap()
    assert registry.envs_selected == ("dev", "prod")


def test_a_selection_is_recorded_without_envs() -> None:
    registry = _run(envs=["prod"]).unwrap()
    assert registry.envs_selected == ("prod",)


def test_a_mistyped_env_is_reported_without_envs() -> None:
    result = _run(envs=["typo"])
    assert isinstance(result, Failure)
    assert "unknown environment 'typo'" in str(result.failure())
