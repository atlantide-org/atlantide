"""Lowering + hashing: deps from Refs, byte-identical IR, stable hash."""

from __future__ import annotations

from typing import Any, ClassVar

from atlantide.core import (
    ProviderRegistry,
    Resource,
    computed,
    immutable,
    mutable,
)
from atlantide.ir import canonical_bytes, hash_ir, lower
from atlantide.lang import evaluate_source
from tests.support import FakeProvider, globals_of


# Minimal, local shapes: these tests assert exact lowered properties, so a rich
# shared resource (with defaulted fields) would only add noise here.
class Bucket(Resource):
    class Meta:
        provider: ClassVar[str] = "test"

    bucket_name: str = immutable()
    tags: dict[str, str] = mutable(default_factory=dict)
    arn: str = computed()


class Notifier(Resource):
    class Meta:
        provider: ClassVar[str] = "test"

    target_arn: str = immutable()


_GLOBALS = globals_of(Bucket, Notifier)

_CONFIG = (
    "b = Bucket('logs', bucket_name='my-logs', tags={'env': 'prod'})\n"
    "Notifier('notify', target_arn=b.arn)\n"
)


def _lower(source: str, providers: ProviderRegistry | None = None) -> Any:
    registry = evaluate_source(source, extra_globals=_GLOBALS).unwrap()
    return lower(registry, providers)


def test_nodes_sorted_and_typed() -> None:
    ir = _lower(_CONFIG)
    assert [n.id for n in ir.nodes] == [
        "default:test.Bucket:logs",
        "default:test.Notifier:notify",
    ]
    bucket = ir.node("default:test.Bucket:logs")
    assert bucket is not None
    assert bucket.type == "test.Bucket"
    assert bucket.properties == {"bucket_name": "my-logs", "tags": {"env": "prod"}}
    # computed fields never appear as inputs
    assert "arn" not in bucket.properties


def test_ref_becomes_dependency_and_marker() -> None:
    ir = _lower(_CONFIG)
    notify = ir.node("default:test.Notifier:notify")
    assert notify is not None
    assert notify.dependencies == ("default:test.Bucket:logs",)
    assert notify.properties == {"target_arn": {"$ref": "default:test.Bucket:logs#arn"}}


def test_provider_version_stamped() -> None:
    providers = ProviderRegistry()
    providers.register(FakeProvider(name="test", version="2.5.1"))
    ir = _lower(_CONFIG, providers)
    assert all(n.provider_version == "2.5.1" for n in ir.nodes)
    # without a registry, version is empty
    assert all(n.provider_version == "" for n in _lower(_CONFIG).nodes)


def test_ir_byte_identical_across_evaluations() -> None:
    ir1 = _lower(_CONFIG)
    ir2 = _lower(_CONFIG)
    assert canonical_bytes(ir1) == canonical_bytes(ir2)
    assert hash_ir(ir1) == hash_ir(ir2)


def test_hash_independent_of_tag_insertion_order() -> None:
    cfg_a = "Bucket('b', bucket_name='x', tags={'a': '1', 'b': '2'})\n"
    cfg_b = "Bucket('b', bucket_name='x', tags={'b': '2', 'a': '1'})\n"
    assert hash_ir(_lower(cfg_a)) == hash_ir(_lower(cfg_b))


def test_hash_changes_with_input() -> None:
    cfg_a = "Bucket('b', bucket_name='x')\n"
    cfg_b = "Bucket('b', bucket_name='y')\n"
    assert hash_ir(_lower(cfg_a)) != hash_ir(_lower(cfg_b))


def test_loop_generates_n_nodes() -> None:
    src = "for i in range(6):\n    Bucket(f'b{i}', bucket_name=f'name-{i}')\n"
    ir = _lower(src)
    assert len(ir) == 6
    assert [n.id for n in ir.nodes] == [f"default:test.Bucket:b{i}" for i in range(6)]


def test_hash_is_hex_sha256() -> None:
    h = hash_ir(_lower(_CONFIG))
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
