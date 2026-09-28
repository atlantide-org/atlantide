"""CloudFront distributions and origin access controls."""

from __future__ import annotations

import boto3
import pytest

from atlantide.core import Context
from atlantide.providers.aws import AwsProvider, CloudFrontDistribution, OriginAccessControl

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_origin_access_control_crud() -> None:
    provider = AwsProvider()
    ctx = Context()
    out = await provider.create(
        ctx, OriginAccessControl("o", oac_name="site-oac", description="v1")
    )
    oid = out["oac_id"]
    assert oid
    # id-located: a resource reconstructed from state carries oac_id.
    tracked = OriginAccessControl("o", oac_name="site-oac", oac_id=oid)
    assert await provider.read(ctx, tracked) is not None
    await provider.update(
        ctx,
        {"oac_id": oid},
        OriginAccessControl("o", oac_name="site-oac", description="v2", oac_id=oid),
    )
    got = boto3.client("cloudfront").get_origin_access_control(Id=oid)
    assert got["OriginAccessControl"]["OriginAccessControlConfig"]["Description"] == "v2"
    await provider.delete(ctx, tracked)
    assert await provider.read(ctx, tracked) is None


async def test_cloudfront_distribution_crud() -> None:
    provider = AwsProvider()
    ctx = Context()
    oac = await provider.create(ctx, OriginAccessControl("o", oac_name="d-oac"))
    origin = "b.s3.us-east-1.amazonaws.com"
    out = await provider.create(
        ctx,
        CloudFrontDistribution(
            "cdn", origin_domain=origin, oac_id=oac["oac_id"], comment="v1", tags={"app": "x"}
        ),
    )
    did = out["distribution_id"]
    assert out["domain_name"].endswith(".cloudfront.net")
    assert out["arn"].startswith("arn:aws:cloudfront:")
    tracked = CloudFrontDistribution(
        "cdn", origin_domain=origin, oac_id=oac["oac_id"], distribution_id=did
    )
    assert await provider.read(ctx, tracked) is not None
    await provider.update(
        ctx,
        {"distribution_id": did},
        CloudFrontDistribution(
            "cdn", origin_domain=origin, oac_id=oac["oac_id"], comment="v2", distribution_id=did
        ),
    )
    cfg = boto3.client("cloudfront").get_distribution(Id=did)["Distribution"]["DistributionConfig"]
    assert cfg["Comment"] == "v2"
    # delete drives disable -> poll-until-Deployed -> delete (moto: Deployed at once,
    # and it does not enforce disable-before-delete, so this only asserts it's gone).
    await provider.delete(ctx, tracked)
    assert await provider.read(ctx, tracked) is None


async def test_cloudfront_distribution_rerun_create_adopts_by_caller_reference() -> None:
    """The stable CallerReference makes a re-run create answer
    DistributionAlreadyExists; the handler adopts the distribution holding the
    reference instead of surfacing the conflict."""
    provider = AwsProvider()
    ctx = Context()
    oac = await provider.create(ctx, OriginAccessControl("o", oac_name="dup-oac"))
    origin = "b.s3.us-east-1.amazonaws.com"
    first = await provider.create(
        ctx, CloudFrontDistribution("cdn", origin_domain=origin, oac_id=oac["oac_id"])
    )

    again = await provider.create(
        ctx, CloudFrontDistribution("cdn", origin_domain=origin, oac_id=oac["oac_id"])
    )

    assert again["distribution_id"] == first["distribution_id"]
    listing = boto3.client("cloudfront").list_distributions()["DistributionList"]
    assert len(listing.get("Items", [])) == 1, "adopted, not duplicated"


# -- custom domains: aliases and a certificate -----------------------------------


def test_a_certificate_can_finally_be_attached_to_a_distribution() -> None:
    """`aliases` and `certificate_arn` produce the distribution's `Aliases` and an
    SNI `ViewerCertificate`, so an `AcmCertificate` can serve a custom domain."""
    from atlantide.providers.aws import CloudFrontDistribution
    from atlantide.providers.aws.handlers.cloudfront import _distribution_config

    config = _distribution_config(
        CloudFrontDistribution(
            "d",
            origin_domain="b.s3.eu-north-1.amazonaws.com",
            oac_id="oac-1",
            aliases=["www.example.com"],
            certificate_arn="arn:aws:acm:us-east-1:1:certificate/abc",
        )
    )

    assert config["Aliases"]["Items"] == ["www.example.com"]
    assert config["ViewerCertificate"]["ACMCertificateArn"].endswith("/abc")
    assert config["ViewerCertificate"]["SSLSupportMethod"] == "sni-only"


def test_a_distribution_without_a_certificate_uses_cloudfronts_own() -> None:
    from atlantide.providers.aws import CloudFrontDistribution
    from atlantide.providers.aws.handlers.cloudfront import _distribution_config

    config = _distribution_config(
        CloudFrontDistribution("d", origin_domain="b.s3.eu-north-1.amazonaws.com", oac_id="oac-1")
    )
    assert config["ViewerCertificate"] == {"CloudFrontDefaultCertificate": True}


def test_aliases_and_a_certificate_must_come_together() -> None:
    """CloudFront rejects an alias with no certificate covering it, and a
    certificate with no alias serves nothing, so both are rejected in config."""
    from atlantide.providers.aws import CloudFrontDistribution

    with pytest.raises(ValueError, match="together"):
        CloudFrontDistribution(
            "d",
            origin_domain="b.s3.x.amazonaws.com",
            oac_id="o",
            aliases=["www.example.com"],
        )
    with pytest.raises(ValueError, match="together"):
        CloudFrontDistribution(
            "d",
            origin_domain="b.s3.x.amazonaws.com",
            oac_id="o",
            certificate_arn="arn:aws:acm:us-east-1:1:certificate/abc",
        )
