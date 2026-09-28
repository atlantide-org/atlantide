"""Regressions: Transform inlining, partial validation of handle-bearing fields,
stack-tag validation, and redaction of log-record args."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from atlantide.core import (
    Resource,
    Stack,
    StackReference,
    collecting,
    inline_stack_outputs,
    mutable,
    output,
)
from atlantide.core.logging import REDACTED, RedactingFilter, get_logger
from atlantide.core.resource import Nested, ResourceRegistry
from atlantide.core.types import Ref, SecretRef, Transform, concat
from atlantide.ir import lower
from atlantide.providers.aws.resources.networking import RouteTable
from atlantide.state import MemoryStateBackend
from tests.support import Tagged, local_engine

from .conftest import Bucket, Notifier


class _Route(Nested):
    cidr: str
    gw: str = ""


class _Routed(Resource):
    class Meta:
        provider: ClassVar[str] = "test"

    routes: list[_Route] = mutable(default_factory=list)


# -- 1. a StackOutputRef inside a Transform is inlined ---------------------


def test_stack_output_ref_inside_transform_is_inlined() -> None:
    with collecting() as reg:
        with Stack("common", region="eu-west-1"):
            bucket = Bucket("logs", bucket_name="logs")
            output("arn", bucket.arn)
        with Stack("app", region="eu-west-1"):
            Notifier("notify", target_arn=concat("x-", StackReference("common").output("arn")))
    notify = inline_stack_outputs(reg).get("app:test.Notifier:notify").unwrap()
    value = notify.input_values()["target_arn"]
    assert value == Transform("concat", ("x-", Ref("common:test.Bucket:logs", "arn")))
    assert notify.refs() == [Ref("common:test.Bucket:logs", "arn")]


def test_output_expression_transform_is_inlined() -> None:
    with collecting() as reg:
        with Stack("common", region="eu-west-1"):
            bucket = Bucket("logs", bucket_name="logs")
            output("arn", bucket.arn)
            output("label", concat("x-", StackReference("common").output("arn")))
        with Stack("app", region="eu-west-1"):
            Notifier("notify", target_arn=StackReference("common").output("label"))
    inlined = inline_stack_outputs(reg)
    expected = Transform("concat", ("x-", Ref("common:test.Bucket:logs", "arn")))
    assert inlined.outputs["common:label"] == expected
    notify = inlined.get("app:test.Notifier:notify").unwrap()
    assert notify.refs() == [Ref("common:test.Bucket:logs", "arn")]


async def test_concat_over_inconfig_stack_output_applies(tmp_path: Path) -> None:
    src, dst = tmp_path / "src.txt", tmp_path / "cfg.txt"
    cfg = (
        "from atlantide.core import Stack, StackReference, output\n"
        "from atlantide.providers.local import File\n"
        "with Stack('common', region='local'):\n"
        f"    src = File('src', path={str(src)!r}, content='hello')\n"
        "    output('cksum', src.checksum)\n"
        "with Stack('app', region='local'):\n"
        f"    File('cfg', path={str(dst)!r}, "
        "content=concat('x-', StackReference('common').output('cksum')))\n"
    )
    engine = local_engine(backend=MemoryStateBackend())
    compiled = engine.compile(cfg).unwrap()
    app = compiled.ir.node("app:local.File:cfg")
    assert app is not None and "common:local.File:src" in app.dependencies
    (await engine.apply(cfg)).unwrap()
    assert dst.read_text() == "x-" + hashlib.sha256(b"hello").hexdigest()


# -- 2. non-handle parts of a handle-bearing field are validated -----------


def test_handle_bearing_list_still_validates_its_other_parts() -> None:
    gw = Ref("default:test.Bucket:a", "arn")
    with collecting(), pytest.raises(ValidationError, match="cidr"):
        _Routed("r", routes=[{"cidr": 5, "gw": gw}])


def test_handle_bearing_list_is_stored_as_written() -> None:
    """The check does not convert: a route dict with a handle stays a dict."""
    gw = Ref("default:test.Bucket:a", "arn")
    raw = [{"cidr": "10.0.0.0/8", "gw": gw}]
    with collecting():
        res = _Routed("r", routes=raw)
    assert res.routes == raw
    assert type(res.routes[0]) is dict
    assert res.refs() == [gw]


def test_route_table_with_a_ref_hashes_as_before() -> None:
    """Canonical form and IR hash match the pre-check storage of the raw value,
    so existing deployments plan no spurious update (no defaults filled in)."""
    igw = Ref("default:aws.InternetGateway:igw", "internet_gateway_id")
    raw = [{"gateway_id": igw}]
    with collecting() as checked:
        table = RouteTable("rt", region="eu-west-1", vpc_id="vpc-1", routes=raw)
    assert table.canonical_inputs()["routes"] == [
        {"gateway_id": {"$ref": "default:aws.InternetGateway:igw#internet_gateway_id"}}
    ]
    # `model_copy` skips validation: exactly how the value was stored before.
    before = ResourceRegistry()
    before.register(table.model_copy(update={"routes": list(raw)}))
    (after_node,), (before_node,) = lower(checked).nodes, lower(before).nodes
    # `to_canonical` is the hashed shape; compare it byte for byte.
    assert json.dumps(after_node.to_canonical(), sort_keys=True) == json.dumps(
        before_node.to_canonical(), sort_keys=True
    )


def test_route_table_still_rejects_a_bad_route_next_to_a_ref() -> None:
    igw = Ref("default:aws.InternetGateway:igw", "internet_gateway_id")
    with collecting(), pytest.raises(ValidationError, match="cidr_block"):
        RouteTable(
            "rt", region="eu-west-1", vpc_id="vpc-1", routes=[{"cidr_block": 5, "gateway_id": igw}]
        )


def test_handle_bearing_dict_rejects_a_wrong_scalar() -> None:
    ref = Ref("default:test.Bucket:a", "arn")
    with collecting(), pytest.raises(ValidationError, match=r"tags\.n"):
        Tagged("t", size=1, tags={"k": ref, "n": 7})


def test_handle_bearing_dict_keeps_its_handles() -> None:
    ref = Ref("default:test.Bucket:a", "arn")
    with collecting():
        res = Tagged("t", size=1, tags={"k": ref, "n": "x"})
    assert res.tags == {"k": ref, "n": "x"}


# -- 3. stack tags are validated -------------------------------------------


def test_stack_tags_are_validated() -> None:
    with (
        collecting(),
        Stack("prod", region="eu-north-1", tags={"cost": 5}),  # type: ignore[dict-item]
        pytest.raises(ValidationError, match="cost"),
    ):
        Tagged("a", size=1)


def test_stack_tags_still_merge_under_own() -> None:
    with collecting(), Stack("prod", region="eu-north-1", tags={"env": "prod", "team": "x"}):
        res = Tagged("a", size=1, tags={"team": "data"})
        bare = Tagged("b", size=1)
    assert res.tags == {"env": "prod", "team": "data"}
    assert bare.tags == {"env": "prod", "team": "x"}


# -- 5. record.args are redacted -------------------------------------------


def _record(msg: str, args: object) -> logging.LogRecord:
    return logging.LogRecord("atlantide.t", logging.INFO, "f", 1, msg, args, None)  # type: ignore[arg-type]


def test_positional_args_are_redacted() -> None:
    record = _record("%s %s", ({"$sealed": "ciphertext"}, "plain"))
    RedactingFilter().filter(record)
    assert record.getMessage() == f"{REDACTED} plain"


def test_mapping_args_are_redacted() -> None:
    record = _record("%(s)s %(n)d", ({"s": SecretRef("api/token"), "n": 3},))
    RedactingFilter().filter(record)
    assert record.getMessage() == f"{REDACTED} 3"


def test_args_without_secrets_are_untouched() -> None:
    record = _record("%s", (("a", "b"),))
    RedactingFilter().filter(record)
    assert record.getMessage() == "('a', 'b')"


def test_lone_marker_arg_is_redacted() -> None:
    """`LogRecord` unwraps a single mapping arg, so the marker is `record.args`."""
    record = _record("%s", ({"$sealed": "ciphertext"},))
    RedactingFilter().filter(record)
    assert record.getMessage() == REDACTED


def test_lone_marker_arg_is_redacted_through_a_logger(caplog: pytest.LogCaptureFixture) -> None:
    log = get_logger("fix-test")
    log.propagate = True
    handler = caplog.handler
    handler.addFilter(RedactingFilter())
    try:
        with caplog.at_level(logging.INFO, logger=log.name):
            log.info("%s", {"$sealed": "ciphertext"})
    finally:
        handler.removeFilter(handler.filters[-1])
    assert "ciphertext" not in caplog.text
    assert REDACTED in caplog.text


def test_unknown_key_next_to_a_handle_is_rejected() -> None:
    gw = Ref("default:test.Bucket:a", "arn")
    with collecting(), pytest.raises(ValidationError, match="typo"):
        _Routed("r", routes=[{"cidr": "10.0.0.0/8", "typo": gw}])
