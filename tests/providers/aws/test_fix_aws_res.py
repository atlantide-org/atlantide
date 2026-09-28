"""Regression tests: AWS resource validation, Lambda packaging, path mutability,
and adoption converging a pre-existing Lambda on its declared configuration."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import boto3
import pytest

from atlantide.core import Context
from atlantide.core.fields import Mutability, field_mutability
from atlantide.providers.aws import (
    AwsProvider,
    IamRole,
    LambdaFunction,
    Route53Record,
    S3Bucket,
    S3Folder,
    SqsQueue,
    Subnet,
    Vpc,
)
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.compute import package_bytes
from atlantide.providers.aws.resources.dns import AliasTarget
from tests.providers.aws.conftest import LAMBDA_TRUST, mixed_engine

pytestmark = pytest.mark.usefixtures("aws_env")

# -- item 1: anchored, ASCII-only patterns ----------------------------------------

ARABIC_INDIC_ONE = chr(0x0661)  # matches \d, not [0-9]


@pytest.mark.parametrize(
    "value",
    ["10.0.0.0/16\n", f"10.0.0.{ARABIC_INDIC_ONE}/16", f"10.0.0.0/{ARABIC_INDIC_ONE}6"],
)
def test_cidr_rejects_trailing_newline_and_non_ascii_digits(value: str) -> None:
    assert v.ipv4_cidr()(value) is not None
    with pytest.raises(ValueError, match="CIDR"):
        Vpc("v", cidr_block=value)
    with pytest.raises(ValueError, match="CIDR"):
        Subnet("s", vpc_id="vpc-1", cidr_block=value)


def test_cidr_still_accepts_valid_blocks() -> None:
    for value in ("10.0.0.0/16", "0.0.0.0/0", "192.168.1.0/32"):
        assert v.ipv4_cidr()(value) is None


def test_domain_rejects_trailing_newline() -> None:
    assert v.domain_name()("example.com\n") is not None
    assert v.domain_name()("example.com") is None
    assert v.domain_name()("example.com.") is None


def test_matches_requires_the_whole_value() -> None:
    import re

    rule = v.matches(re.compile(r"^[a-z]+$"), "thing", "lowercase")
    assert rule("abc") is None
    assert rule("abc\n") is not None


def test_bucket_name_rejects_trailing_newline() -> None:
    with pytest.raises(ValueError, match="S3 bucket name"):
        S3Bucket("b", bucket="my-bucket\n")


def test_sqs_name_rejects_trailing_newline_and_non_ascii() -> None:
    with pytest.raises(ValueError, match="SQS queue name"):
        SqsQueue("q", queue_name="jobs\n")
    with pytest.raises(ValueError, match="SQS queue name"):
        SqsQueue("q", queue_name=f"jobs{ARABIC_INDIC_ONE}")


# -- item 2: deterministic, faithful Lambda packages ------------------------------


def _tree(root: Path) -> Path:
    (root / "lib").mkdir(parents=True)
    (root / "index.py").write_text("def handler(e, c): pass\n")
    (root / "lib" / "util.py").write_text("X = 1\n")
    return root


def test_package_skips_python_caches(tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    clean = package_bytes(root)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "index.cpython-312.pyc").write_bytes(b"\x00junk")
    (root / "lib" / "stray.pyc").write_bytes(b"\x00junk")

    assert package_bytes(root) == clean
    names = zipfile.ZipFile(io.BytesIO(package_bytes(root))).namelist()
    assert names == ["index.py", "lib/util.py"]


def test_package_uses_posix_names(tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    names = zipfile.ZipFile(io.BytesIO(package_bytes(root))).namelist()
    assert "lib/util.py" in names
    assert all("\\" not in name for name in names)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")
def test_package_keeps_the_exec_bit(tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    bootstrap = root / "bootstrap"
    bootstrap.write_text("#!/bin/sh\n")
    bootstrap.chmod(0o755)

    archive = zipfile.ZipFile(io.BytesIO(package_bytes(root)))
    modes = {info.filename: (info.external_attr >> 16) & 0o777 for info in archive.infolist()}
    assert modes["bootstrap"] == 0o755
    assert modes["index.py"] == 0o644

    # The mode is normalised, so a group/other bit alone does not change the hash.
    before = package_bytes(root)
    bootstrap.chmod(0o700)
    assert package_bytes(root) == before
    bootstrap.chmod(0o644)
    assert package_bytes(root) != before


# -- item 3: moving the source is an in-place update, not a replacement ------------


def test_source_paths_are_mutable() -> None:
    assert field_mutability(LambdaFunction)["code_path"] is Mutability.MUTABLE
    assert field_mutability(S3Folder)["source_path"] is Mutability.MUTABLE


async def test_moving_a_folder_source_updates_without_reupload(tmp_path: Path) -> None:
    engine = mixed_engine()
    old = tmp_path / "old"
    old.mkdir()
    (old / "index.html").write_text("v1")
    config = (
        "from atlantide.providers.aws import S3Bucket, S3Folder\n"
        "b = S3Bucket('site', bucket='move-site')\n"
        f"S3Folder('assets', bucket=b.name, source_path={str(old)!r})\n"
    )
    (await engine.apply(config)).unwrap()

    new = tmp_path / "new"
    old.rename(new)
    report = (await engine.apply(config.replace(str(old), str(new)))).unwrap()
    assert "default:aws.S3Folder:assets" in report.updated
    assert "default:aws.S3Folder:assets" not in report.replaced
    body = boto3.client("s3").get_object(Bucket="move-site", Key="index.html")["Body"].read()
    assert body == b"v1"

    # A later content change is read from the new location.
    (new / "index.html").write_text("v2")
    (await engine.apply(config.replace(str(old), str(new)))).unwrap()
    body = boto3.client("s3").get_object(Bucket="move-site", Key="index.html")["Body"].read()
    assert body == b"v2"


async def test_lambda_update_reads_code_from_the_new_path(tmp_path: Path) -> None:
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="move-role", assume_role_policy=LAMBDA_TRUST)
    )
    old = _tree(tmp_path / "old")
    out = await provider.create(
        Context(),
        LambdaFunction("f", function_name="mv", role_arn=role["arn"], code_path=str(old)),
    )
    new = tmp_path / "new"
    old.rename(new)
    moved = LambdaFunction("f", function_name="mv", role_arn=role["arn"], code_path=str(new))
    assert (await provider.update(Context(), out, moved))["arn"] == out["arn"]


# -- item 4: Route53Record takes exactly one of records / alias --------------------

_ALIAS = AliasTarget(name="d111.cloudfront.net", zone_id="Z2FDTNDATAQYW2")


def test_record_requires_records_or_alias() -> None:
    with pytest.raises(ValueError, match="records or alias"):
        Route53Record("r", zone_id="Z1", record_name="www.example.com")


def test_alias_record_rejects_a_ttl() -> None:
    with pytest.raises(ValueError, match="ttl"):
        Route53Record("r", zone_id="Z1", record_name="example.com", alias=_ALIAS, ttl=60)


def test_valid_records_pass() -> None:
    Route53Record("r", zone_id="Z1", record_name="example.com", alias=_ALIAS)
    Route53Record("r2", zone_id="Z1", record_name="www.example.com", records=["1.2.3.4"], ttl=60)


# -- item 5: a .fifo suffix needs fifo=True ----------------------------------------


def test_fifo_suffix_requires_fifo() -> None:
    with pytest.raises(ValueError, match="fifo"):
        SqsQueue("q", queue_name="jobs.fifo")
    SqsQueue("q", queue_name="jobs.fifo", fifo=True)
    SqsQueue("q2", queue_name="jobs", fifo=True)


# -- item 11: an adopted Lambda converges on the declared configuration ------------


async def test_adopted_lambda_gets_the_declared_configuration(tmp_path: Path) -> None:
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="adopt-cfg-role", assume_role_policy=LAMBDA_TRUST)
    )
    src = _tree(tmp_path / "src")
    # A function left behind with other settings (e.g. by an interrupted apply).
    await provider.create(
        Context(),
        LambdaFunction(
            "f", function_name="adopt-cfg", role_arn=role["arn"], code_path=str(src), timeout=3
        ),
    )
    declared = LambdaFunction(
        "f",
        function_name="adopt-cfg",
        role_arn=role["arn"],
        code_path=str(src),
        timeout=30,
        memory_size=256,
        environment={"MODE": "prod"},
    )
    out = await provider.create(Context(), declared)
    assert out["arn"].endswith(":function:adopt-cfg")
    cfg = boto3.client("lambda").get_function(FunctionName="adopt-cfg")["Configuration"]
    assert cfg["Timeout"] == 30
    assert cfg["MemorySize"] == 256
    assert cfg["Environment"]["Variables"] == {"MODE": "prod"}
