"""ACM certificates."""

from __future__ import annotations

import pytest

from atlantide.core import Context
from atlantide.providers.aws import AcmCertificate, AwsProvider

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_acm_certificate_crud() -> None:
    provider = AwsProvider()
    ctx = Context()
    out = await provider.create(
        ctx, AcmCertificate("cert", domain_name="ex.example.com", tags={"app": "x"})
    )
    arn = out["arn"]
    assert arn.startswith("arn:aws:acm:us-east-1:")  # handler pins us-east-1
    assert out["validation_type"] == "CNAME"
    assert out["validation_name"] and out["validation_value"]
    tracked = AcmCertificate("cert", domain_name="ex.example.com", arn=arn)
    assert await provider.read(ctx, tracked) is not None
    await provider.delete(ctx, tracked)
    assert await provider.read(ctx, tracked) is None
