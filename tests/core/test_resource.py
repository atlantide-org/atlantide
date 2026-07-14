"""Resource identity, validation, Ref semantics, canonical inputs, registry."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlantide.core import Ref, RegistryError, Stack, collecting, is_successful, output

from .conftest import Bucket, Notifier


def test_node_id_and_type() -> None:
    b = Bucket("logs", bucket_name="my-logs")
    assert Bucket.type_name() == "test.Bucket"
    assert b.node_id == "default:test.Bucket:logs"
    assert b.logical_name == "logs"


def test_invalid_logical_name_rejected() -> None:
    with pytest.raises(RegistryError, match="invalid resource name"):
        Bucket("9bad name", bucket_name="x")


def test_typed_validation_still_enforced() -> None:
    with pytest.raises(ValidationError):
        Bucket("logs", bucket_name="x", versioning="definitely-not-a-bool")
    with pytest.raises(ValidationError):
        Bucket("logs", bucket_name="x", unknown_field=1)


def test_computed_field_reads_as_ref() -> None:
    b = Bucket("logs", bucket_name="my-logs")
    ref = b.arn
    assert isinstance(ref, Ref)
    assert ref == Ref(node_id="default:test.Bucket:logs", attr="arn")


def test_ref_flows_into_dependent_resource() -> None:
    b = Bucket("logs", bucket_name="my-logs")
    n = Notifier("notify", target_arn=b.arn)
    refs = n.refs()
    assert refs == [Ref(node_id="default:test.Bucket:logs", attr="arn")]


def test_refs_found_inside_containers() -> None:
    b = Bucket("logs", bucket_name="my-logs")
    b2 = Bucket("other", bucket_name="x", tags={"upstream": b.arn})
    assert b2.refs() == [Ref(node_id="default:test.Bucket:logs", attr="arn")]


def test_canonical_inputs() -> None:
    b = Bucket("logs", bucket_name="my-logs")
    n = Notifier("notify", target_arn=b.arn, message="hi")
    assert n.canonical_inputs() == {
        "target_arn": {"$ref": "default:test.Bucket:logs#arn"},
        "message": "hi",
    }
    # computed fields never appear in inputs
    assert "arn" not in b.canonical_inputs()


def test_lifecycle_default_and_override() -> None:
    from atlantide.core import Lifecycle

    b = Bucket("logs", bucket_name="x")
    assert b.lifecycle.prevent_destroy is False
    b2 = Bucket("keep", bucket_name="y", lifecycle=Lifecycle(prevent_destroy=True))
    assert b2.lifecycle.prevent_destroy is True


def test_collecting_registry_auto_registers() -> None:
    with collecting() as reg:
        b = Bucket("logs", bucket_name="x")
        Notifier("notify", target_arn=b.arn)
    assert len(reg) == 2
    assert "default:test.Bucket:logs" in reg
    assert reg.get("default:test.Bucket:logs").unwrap() is b
    assert not is_successful(reg.get("default:test.Bucket:missing"))
    assert [r.node_id for r in reg.all()] == sorted(r.node_id for r in reg.all())


def test_duplicate_registration_rejected() -> None:
    with collecting(), pytest.raises(RegistryError, match="duplicate resource"):
        Bucket("logs", bucket_name="a")
        Bucket("logs", bucket_name="b")


def test_no_registration_outside_collecting() -> None:
    with collecting() as reg:
        Bucket("inside", bucket_name="x")
    Bucket("outside", bucket_name="y")  # no active registry: fine, unregistered
    assert len(reg) == 1


def test_output_records_into_registry_namespaced_by_stack() -> None:
    with collecting() as reg:
        b = Bucket("logs", bucket_name="x")
        output("bucket_arn", b.arn)
        output("literal", "v1")
        with Stack("prod", region="us-east-1"):
            output("bucket_arn", b.arn)  # same name, different stack -> no collision
    outputs = reg.outputs
    assert set(outputs) == {"default:bucket_arn", "default:literal", "prod:bucket_arn"}
    assert isinstance(outputs["default:bucket_arn"], Ref)
    assert outputs["default:literal"] == "v1"


def test_output_outside_collecting_raises() -> None:
    with pytest.raises(RegistryError, match="output\\(\\) must be called"):
        output("x", "y")


def test_duplicate_output_rejected() -> None:
    with collecting(), pytest.raises(RegistryError, match="duplicate output"):
        output("dup", 1)
        output("dup", 2)
