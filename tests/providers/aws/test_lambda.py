"""Lambda functions: CRUD, adoption, and the code package that ships."""

from __future__ import annotations

import io
from pathlib import Path

import boto3
import pytest

from atlantide.core import Context
from atlantide.core.errors import LanguageError, ProviderError
from atlantide.providers.aws import AwsProvider, IamRole, LambdaFunction
from atlantide.providers.aws.resources.compute import package_bytes
from tests.providers.aws.conftest import LAMBDA_TRUST

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_lambda_create_read_update_delete(package: str) -> None:
    provider = AwsProvider()
    role_out = await provider.create(
        Context(), IamRole("r", role_name="fn-role", assume_role_policy=LAMBDA_TRUST)
    )
    res = LambdaFunction(
        "f",
        function_name="fn",
        role_arn=role_out["arn"],
        tags={"env": "t"},
        code_path=package,
    )
    out = await provider.create(Context(), res)
    assert out["arn"].endswith(":function:fn")
    assert await provider.read(Context(), res) is not None

    await provider.update(
        Context(),
        out,
        LambdaFunction(
            "f",
            function_name="fn",
            role_arn=role_out["arn"],
            runtime="python3.11",
            code_path=package,
        ),
    )
    cfg = boto3.client("lambda").get_function(FunctionName="fn")["Configuration"]
    assert cfg["Runtime"] == "python3.11"

    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_lambda_create_adopts_instead_of_erroring(package: str) -> None:
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="adopt-role", assume_role_policy=LAMBDA_TRUST)
    )
    fn = LambdaFunction("f", function_name="adopt-fn", role_arn=role["arn"], code_path=package)
    first = await provider.create(Context(), fn)
    assert await provider.create(Context(), fn) == first


# -- code source -----------------------------------------------------------------


async def test_a_lambda_with_no_code_is_refused(package: str) -> None:
    """A function without code is refused rather than shipped with a placeholder.

    A placeholder zip deploys and reports success, then fails at the first
    invocation, after every earlier signal said it worked.
    """
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="nocode-role", assume_role_policy=LAMBDA_TRUST)
    )
    fn = LambdaFunction("f", function_name="nocode", role_arn=role["arn"])

    with pytest.raises(ProviderError, match="has no code"):
        await provider.create(Context(), fn)


async def test_the_deployed_bytes_are_the_ones_on_disk(package: str) -> None:
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="real-role", assume_role_policy=LAMBDA_TRUST)
    )
    (Path(package) / "index.py").write_text("def handler(e, c):\n    return 'mine'\n")
    fn = LambdaFunction("f", function_name="real", role_arn=role["arn"], code_path=package)
    await provider.create(Context(), fn)

    import zipfile as _zip

    shipped = boto3.client("lambda").get_function(FunctionName="real")
    assert shipped["Configuration"]["FunctionName"] == "real"
    # The package the resource fingerprinted is a zip of what is on disk.
    archive = _zip.ZipFile(io.BytesIO(package_bytes(Path(package))))
    assert archive.read("index.py").decode() == "def handler(e, c):\n    return 'mine'\n"


def test_the_fingerprint_changes_with_the_code(tmp_path: Path) -> None:
    """`code_sha256` is the input the diff watches, so it has to move when a byte
    does — otherwise a code change plans as NOOP and never ships."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "index.py").write_text("one")
    first = LambdaFunction(
        "f", function_name="fp", role_arn="arn:x", region="eu-north-1", code_path=str(source)
    ).code_sha256

    (source / "index.py").write_text("two")
    second = LambdaFunction(
        "f", function_name="fp", role_arn="arn:x", region="eu-north-1", code_path=str(source)
    ).code_sha256

    assert first != second


def test_the_fingerprint_is_stable_for_identical_trees(tmp_path: Path) -> None:
    """Two checkouts of the same code must fingerprint alike, or every plan on a
    fresh clone shows a spurious update. File mtimes differ between checkouts,
    which is why the zip pins its timestamps."""
    contents = {"index.py": "def handler(e, c): pass\n", "lib/util.py": "X = 1\n"}
    digests = []
    for nth in ("a", "b"):
        root = tmp_path / nth
        for name, text in contents.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        digests.append(
            LambdaFunction(
                "f",
                function_name="fp",
                role_arn="arn:x",
                region="eu-north-1",
                code_path=str(root),
            ).code_sha256
        )
    assert digests[0] == digests[1]


def test_a_missing_code_path_is_caught_at_config_time(tmp_path: Path) -> None:
    """Before any provider call, so the error names the config rather than
    arriving half-way through an apply."""
    with pytest.raises(LanguageError, match="does not exist"):
        LambdaFunction(
            "f",
            function_name="fp",
            role_arn="arn:x",
            region="eu-north-1",
            code_path=str(tmp_path / "nope"),
        )


def test_code_path_and_s3_bucket_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(LanguageError, match="not both"):
        LambdaFunction(
            "f",
            function_name="fp",
            role_arn="arn:x",
            region="eu-north-1",
            code_path=str(tmp_path),
            s3_bucket="b",
            s3_key="k",
        )


def test_an_s3_source_needs_a_key() -> None:
    with pytest.raises(LanguageError, match="s3_key"):
        LambdaFunction(
            "f", function_name="fp", role_arn="arn:x", region="eu-north-1", s3_bucket="b"
        )


async def test_a_lambda_read_reports_the_config_it_can_check(package: str) -> None:
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="obs-role", assume_role_policy=LAMBDA_TRUST)
    )
    fn = LambdaFunction(
        "f",
        function_name="observed",
        role_arn=role["arn"],
        code_path=package,
        memory_size=512,
        timeout=42,
    )
    await provider.create(Context(), fn)

    live = await provider.read(Context(), fn)

    assert live is not None
    assert live["memory_size"] == 512
    assert live["timeout"] == 42
    assert live["handler"] == "index.handler"
