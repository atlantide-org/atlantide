"""Inlining in-config cross-stack output refs into real Refs (or leaving external)."""

from __future__ import annotations

import pytest

from atlantide.core import (
    Ref,
    Stack,
    StackOutputCycleError,
    StackOutputRef,
    StackReference,
    collecting,
    inline_stack_outputs,
    output,
)

from .conftest import Bucket, Notifier


def _common_bucket_and_app(target: object) -> tuple[Bucket, Notifier]:
    """`common` exports its bucket arn; `app` consumes ``target`` into a Notifier."""
    with Stack("common", region="eu-west-1"):
        bucket = Bucket("logs", bucket_name="logs")
        output("bucket_arn", bucket.arn)
    with Stack("app", region="eu-west-1"):
        notifier = Notifier("notify", target_arn=target)
    return bucket, notifier


def test_inconfig_ref_becomes_real_ref() -> None:
    with collecting() as reg:
        _common_bucket_and_app(StackReference("common").output("bucket_arn"))
    inlined = inline_stack_outputs(reg)
    notify = inlined.get("app:test.Notifier:notify").unwrap()
    assert notify.input_values()["target_arn"] == Ref("common:test.Bucket:logs", "arn")
    assert notify.refs() == [Ref("common:test.Bucket:logs", "arn")]


def test_external_ref_left_untouched() -> None:
    # `other` is not a stack in this config -> external reference, unchanged.
    with collecting() as reg, Stack("app", region="eu-west-1"):
        Notifier("notify", target_arn=StackReference("other").output("z"))
    inlined = inline_stack_outputs(reg)
    value = inlined.get("app:test.Notifier:notify").unwrap().input_values()["target_arn"]
    assert value == StackOutputRef("other", "z")
    # Nothing to inline -> the same registry instance is returned.
    assert inlined is reg


def test_literal_output_inlines_as_value_no_edge() -> None:
    with collecting() as reg:
        with Stack("common", region="eu-west-1"):
            output("name", "prod-v1")
        with Stack("app", region="eu-west-1"):
            Notifier("notify", target_arn=StackReference("common").output("name"))
    inlined = inline_stack_outputs(reg)
    notify = inlined.get("app:test.Notifier:notify").unwrap()
    assert notify.input_values()["target_arn"] == "prod-v1"
    assert notify.refs() == []


def test_composite_output_preserves_inner_refs() -> None:
    # The *output* is a composite (a dict embedding a Ref); a downstream field
    # references the whole output. Inlining must keep every inner Ref.
    with collecting() as reg:
        with Stack("common", region="eu-west-1"):
            bucket = Bucket("logs", bucket_name="logs")
            output("tagset", {"upstream": bucket.arn})
        with Stack("app", region="eu-west-1"):
            Bucket("mirror", bucket_name="mirror", tags=StackReference("common").output("tagset"))
    inlined = inline_stack_outputs(reg)
    mirror = inlined.get("app:test.Bucket:mirror").unwrap()
    assert mirror.input_values()["tags"] == {"upstream": Ref("common:test.Bucket:logs", "arn")}
    assert mirror.refs() == [Ref("common:test.Bucket:logs", "arn")]


def test_transitive_chain_resolves_to_source() -> None:
    with collecting() as reg:
        with Stack("c", region="eu-west-1"):
            output("c_out", "deep")
        with Stack("b", region="eu-west-1"):
            output("b_out", StackReference("c").output("c_out"))
        with Stack("app", region="eu-west-1"):
            Notifier("notify", target_arn=StackReference("b").output("b_out"))
    inlined = inline_stack_outputs(reg)
    assert inlined.get("app:test.Notifier:notify").unwrap().input_values()["target_arn"] == "deep"
    # the intermediate output is resolved too, so committed state carries the value.
    assert inlined.outputs["b:b_out"] == "deep"


def test_transitive_chain_over_refs_creates_edge() -> None:
    with collecting() as reg:
        with Stack("c", region="eu-west-1"):
            bucket = Bucket("logs", bucket_name="logs")
            output("c_out", bucket.arn)
        with Stack("b", region="eu-west-1"):
            output("b_out", StackReference("c").output("c_out"))
        with Stack("app", region="eu-west-1"):
            Notifier("notify", target_arn=StackReference("b").output("b_out"))
    inlined = inline_stack_outputs(reg)
    assert inlined.get("app:test.Notifier:notify").unwrap().refs() == [
        Ref("c:test.Bucket:logs", "arn")
    ]


def test_cycle_detected() -> None:
    with collecting() as reg:
        with Stack("a", region="eu-west-1"):
            output("x", StackReference("b").output("y"))
        with Stack("b", region="eu-west-1"):
            output("y", StackReference("a").output("x"))
    with pytest.raises(StackOutputCycleError, match="a:x -> b:y -> a:x"):
        inline_stack_outputs(reg)


def test_node_id_and_stack_preserved_across_copy() -> None:
    with collecting() as reg:
        _common_bucket_and_app(StackReference("common").output("bucket_arn"))
    inlined = inline_stack_outputs(reg)
    copied = inlined.get("app:test.Notifier:notify").unwrap()
    assert copied.node_id == "app:test.Notifier:notify"
    assert copied.stack == "app"
    assert copied.logical_name == "notify"
