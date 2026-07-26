"""Components: child namespacing, no-collision, and expansion into flat nodes."""

from __future__ import annotations

from typing import ClassVar

from atlantide.core import Component, Resource, Stack, immutable
from atlantide.core.resource import collecting


class _Thing(Resource):
    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable(default=0)


class Pair(Component):
    """Two children, one referencing the other's computed-free identity."""

    def __init__(self, name: str, *, size: int) -> None:
        self.a = _Thing("a", size=size)
        self.b = _Thing("b", size=size + 1)


def test_children_are_namespaced_by_component_name() -> None:
    with collecting() as reg, Stack("prod", region="eu-north-1"):
        pair = Pair("web", size=1)
    ids = sorted(r.node_id for r in reg.all())
    assert ids == ["prod:test._Thing:web-a", "prod:test._Thing:web-b"]
    assert pair.a.node_id == "prod:test._Thing:web-a"
    assert pair.name == "web"


def test_two_instances_do_not_collide() -> None:
    with collecting() as reg, Stack("prod", region="eu-north-1"):
        Pair("web", size=1)
        Pair("docs", size=1)
    ids = sorted(r.node_id for r in reg.all())
    assert ids == [
        "prod:test._Thing:docs-a",
        "prod:test._Thing:docs-b",
        "prod:test._Thing:web-a",
        "prod:test._Thing:web-b",
    ]


class Extended(Pair):
    """Subclass whose ``__init__`` chains to the parent's via ``super()``."""

    def __init__(self, name: str, *, size: int) -> None:
        super().__init__(name, size=size)
        self.c = _Thing("c", size=size + 2)


def test_super_init_pushes_the_prefix_once() -> None:
    """Both inits are wrapped, so without the re-entrancy guard the chained call
    would push the name twice and children would land at ``web-web-a``."""
    with collecting() as reg, Stack("prod", region="eu-north-1"):
        ext = Extended("web", size=1)
        after = _Thing("standalone", size=9)
    ids = sorted(r.node_id for r in reg.all())
    assert ids == [
        "prod:test._Thing:standalone",
        "prod:test._Thing:web-a",
        "prod:test._Thing:web-b",
        "prod:test._Thing:web-c",
    ]
    assert ext.name == "web"
    assert after.node_id == "prod:test._Thing:standalone"  # prefix fully restored


def test_prefix_is_restored_after_component() -> None:
    with collecting() as reg, Stack("prod", region="eu-north-1"):
        Pair("web", size=1)
        top = _Thing("standalone", size=9)  # created after the component
    assert top.node_id == "prod:test._Thing:standalone"  # unprefixed
    assert len(reg.all()) == 3
