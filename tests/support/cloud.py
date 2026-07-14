"""Cloud-provider test kit: reusable env + mock + default Stack setup.

Adding a new cloud provider's suite is three lines — supply the provider's env
vars and a ``mock_factory`` (e.g. ``moto.mock_aws``); everything else is shared.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager

import pytest

from atlantide.core import Stack


def cloud_env_fixture(
    env: dict[str, str],
    *,
    region: str,
    mock_factory: Callable[[], AbstractContextManager[object]],
    stack: str = "default",
) -> Callable[..., Iterator[None]]:
    """An autouse pytest fixture bound to a suite's env + mock + default Stack.

    ``mock_factory`` is the seam: ``moto.mock_aws`` for AWS, any context-manager
    factory for the next provider.
    """

    @pytest.fixture(autouse=True)
    def _fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with mock_factory(), Stack(stack, region=region):
            yield

    return _fixture
