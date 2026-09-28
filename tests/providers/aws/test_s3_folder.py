"""S3Folder: a directory synced to a bucket prefix, keyed on a content manifest."""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest

from atlantide.core import Context, Stack
from atlantide.core.errors import LanguageError
from atlantide.providers.aws import AwsProvider, Region, S3Bucket, S3Folder
from tests.providers.aws.conftest import mixed_engine

pytestmark = pytest.mark.usefixtures("aws_env")


def _site(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def _objects(bucket: str, prefix: str = "") -> dict[str, str]:
    client = boto3.client("s3")
    resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return {
        obj["Key"]: client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
        for obj in resp.get("Contents", [])
    }


def test_s3folder_manifest_is_deterministic_and_excludes_caches(tmp_path: Path) -> None:
    root = _site(tmp_path, {"index.html": "hi", "css/app.css": "body{}"})
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_text("junk")

    folder = S3Folder("assets", bucket="b", source_path=str(root))
    assert set(folder.manifest) == {"index.html", "css/app.css"}  # posix rels, no caches
    # Re-reading the same tree yields an identical manifest.
    assert S3Folder("a2", bucket="b", source_path=str(root)).manifest == folder.manifest


def test_s3folder_source_path_must_be_literal() -> None:
    ref_path = S3Bucket("b", bucket="some-bucket").regional_domain_name  # a Ref
    with pytest.raises(LanguageError, match="literal directory"):
        S3Folder("assets", bucket="b", source_path=ref_path)


def test_s3folder_pinned_manifest_skips_disk() -> None:
    # A rehydrate (deploy) passes a pinned manifest and must not touch disk.
    folder = S3Folder(
        "assets", bucket="b", source_path="/does/not/exist", manifest={"a.txt": "abc123"}
    )
    assert folder.manifest == {"a.txt": "abc123"}


def test_s3folder_bucket_name_creates_dependency_edge(tmp_path: Path) -> None:
    from atlantide.core import collecting
    from atlantide.ir import lower

    root = _site(tmp_path, {"index.html": "hi"})
    with collecting() as reg, Stack("s", region=Region.UsEast1):
        b = S3Bucket("site", bucket="dep-site")
        folder = S3Folder("assets", bucket=b.name, source_path=str(root))
    node = lower(reg).node(folder.node_id)
    assert node is not None
    assert b.node_id in node.dependencies  # b.name (a Ref) orders folder after bucket


async def test_s3folder_create_uploads_all(tmp_path: Path) -> None:
    root = _site(tmp_path, {"index.html": "<h1>hi</h1>", "css/app.css": "body{}"})
    provider = AwsProvider()
    boto3.client("s3").create_bucket(Bucket="site")

    out = await provider.create(
        Context(), S3Folder("assets", bucket="site", source_path=str(root), prefix="web/")
    )
    assert set(out["uploaded"]) == {"web/index.html", "web/css/app.css"}
    assert _objects("site") == {"web/index.html": "<h1>hi</h1>", "web/css/app.css": "body{}"}
    # Content-Type is inferred from the key's extension.
    head = boto3.client("s3").head_object(Bucket="site", Key="web/index.html")
    assert head["ContentType"] == "text/html"


async def test_s3folder_update_syncs_delta_and_prunes(tmp_path: Path) -> None:
    provider = AwsProvider()
    boto3.client("s3").create_bucket(Bucket="site")
    root = _site(tmp_path, {"index.html": "v1", "app.css": "body{}", "old.txt": "bye"})
    prior = await provider.create(
        Context(), S3Folder("assets", bucket="site", source_path=str(root), prefix="web/")
    )

    # Change index.html, add main.js, remove old.txt.
    (root / "index.html").write_text("v2")
    (root / "main.js").write_text("console.log(1)")
    (root / "old.txt").unlink()
    updated = S3Folder("assets", bucket="site", source_path=str(root), prefix="web/")

    out = await provider.update(Context(), prior, updated)
    assert set(out["uploaded"]) == {"web/index.html", "web/app.css", "web/main.js"}
    assert _objects("site") == {
        "web/index.html": "v2",
        "web/app.css": "body{}",
        "web/main.js": "console.log(1)",
    }  # old.txt pruned


async def test_s3folder_delete_removes_objects(tmp_path: Path) -> None:
    provider = AwsProvider()
    boto3.client("s3").create_bucket(Bucket="site")
    root = _site(tmp_path, {"index.html": "hi", "a.css": "x"})
    res = S3Folder("assets", bucket="site", source_path=str(root), prefix="web/")
    out = await provider.create(Context(), res)

    # State restores the computed ``uploaded`` map onto the resource for delete.
    res.uploaded = out["uploaded"]  # type: ignore[misc]
    await provider.delete(Context(), res)
    assert _objects("site") == {}


async def test_s3folder_read_missing_bucket_is_none() -> None:
    provider = AwsProvider()
    res = S3Folder("assets", bucket="ghost", source_path="/tmp", manifest={})
    assert await provider.read(Context(), res) is None


async def test_s3folder_through_engine_noop_update_replace(tmp_path: Path) -> None:
    engine = mixed_engine()
    root = _site(tmp_path, {"index.html": "v1"})
    config = (
        "from atlantide.providers.aws import S3Bucket, S3Folder\n"
        "b = S3Bucket('site', bucket='eng-site')\n"
        # bucket=b.name orders the folder after the bucket (a literal name would not).
        f"S3Folder('assets', bucket=b.name, source_path={str(root)!r}, prefix='web/')\n"
    )

    report = (await engine.apply(config)).unwrap()
    assert len(report.created) == 2
    assert _objects("eng-site") == {"web/index.html": "v1"}

    # Re-apply unchanged -> Merkle NOOP for both nodes.
    report2 = (await engine.apply(config)).unwrap()
    assert len(report2.noop) == 2

    # Edit a file on disk -> manifest changes -> S3Folder UPDATE (bucket unchanged).
    (root / "index.html").write_text("v2")
    report3 = (await engine.apply(config)).unwrap()
    assert "default:aws.S3Folder:assets" in report3.updated
    assert _objects("eng-site") == {"web/index.html": "v2"}

    # Change the immutable prefix -> REPLACE.
    replaced = config.replace("prefix='web/'", "prefix='static/'")
    report4 = (await engine.apply(replaced)).unwrap()
    assert "default:aws.S3Folder:assets" in report4.replaced
