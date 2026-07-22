"""Shared test harness.

Import helpers from here instead of re-authoring per suite::

    from tests.support import FakeProvider, Harness, types_of, Box

**Adding a new provider's test suite:** define the provider's resource classes
(or reuse :mod:`tests.support.resources`), a provider under test (or a
:class:`FakeProvider`), derive TYPES with :func:`types_of`, and — for a cloud
provider needing credentials/mocks — wire setup with
:func:`cloud_env_fixture` (supply the provider's env vars and a ``mock_factory``).
Drive scenarios through :class:`Harness` or :func:`engine_for`.
"""

from __future__ import annotations

from tests.support.clock import FakeClock
from tests.support.cloud import (
    TEST_REGION,
    cloud_env_fixture,
    create_state_store,
    fake_aws_credentials,
)
from tests.support.factories import (
    engine_for,
    globals_of,
    state_node,
    types_of,
)
from tests.support.harness import Harness
from tests.support.providers import FakeProvider, OutputSpec, default_outputs
from tests.support.resources import (
    Box,
    Bucket,
    Notifier,
    Server,
    Tagged,
    Thing,
    Vault,
    Widget,
)

__all__ = [
    "TEST_REGION",
    "Box",
    "Bucket",
    "FakeClock",
    "FakeProvider",
    "Harness",
    "Notifier",
    "OutputSpec",
    "Server",
    "Tagged",
    "Thing",
    "Vault",
    "Widget",
    "cloud_env_fixture",
    "create_state_store",
    "default_outputs",
    "engine_for",
    "fake_aws_credentials",
    "globals_of",
    "state_node",
    "types_of",
]
