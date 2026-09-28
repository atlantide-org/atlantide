"""Regressions for the AWS service-handler fix round (slug aws-svc)."""

from __future__ import annotations

from typing import Any

import boto3
import pytest

from atlantide.core import Context
from atlantide.core.errors import ProviderError
from atlantide.providers.aws import (
    AcmCertificate,
    AwsProvider,
    DynamoDbTable,
    IamRole,
    OriginAccessControl,
    Route53HostedZone,
    Route53Record,
    SnsTopic,
    SqsQueue,
)
from atlantide.providers.aws.handlers import certificate
from atlantide.providers.aws.handlers.dns import Route53RecordHandler
from tests.providers.aws.conftest import TRUST_POLICY, client_error

pytestmark = pytest.mark.usefixtures("aws_env")


# -- 1. DynamoDB billing mode ------------------------------------------------------


def test_provisioned_billing_is_refused_at_validation() -> None:
    """PROVISIONED needs capacity fields the resource does not have, so AWS would
    reject every create; it is refused during plan instead."""
    with pytest.raises(ValueError, match=r"PROVISIONED.*PAY_PER_REQUEST"):
        DynamoDbTable("d", table_name="t", hash_key="id", billing_mode="PROVISIONED")


# -- 2. DynamoDB delete waits for the table to be gone -------------------------------


async def test_dynamodb_delete_waits_for_the_table_to_be_gone() -> None:
    provider = AwsProvider()
    res = DynamoDbTable("d", table_name="dying", hash_key="id")
    await provider.create(Context(), res)
    _handler, client = provider._dispatch(res, "delete")
    waited: list[str] = []
    real = client.get_waiter

    def spy(name: str) -> Any:
        waited.append(name)
        return real(name)

    client.get_waiter = spy
    await provider.delete(Context(), res)
    assert waited == ["table_not_exists"]


# -- 3. OAC create is idempotent ----------------------------------------------------


async def test_a_retried_oac_create_adopts_the_existing_control() -> None:
    provider = AwsProvider()
    res = OriginAccessControl("o", oac_name="retry-oac")
    out = await provider.create(Context(), res)
    _handler, client = provider._dispatch(res, "create")

    def exists(**_kw: Any) -> None:
        raise client_error("OriginAccessControlAlreadyExists")

    client.create_origin_access_control = exists
    assert await provider.create(Context(), res) == out


# -- 4/5. ACM validation record and idempotency token -------------------------------


async def test_acm_create_polls_until_the_validation_record_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AwsProvider()
    res = AcmCertificate("c", domain_name="poll.example.com")
    _handler, client = provider._dispatch(res, "create")
    real = client.describe_certificate
    calls = {"n": 0}

    def late(**kw: Any) -> Any:
        calls["n"] += 1
        got = real(**kw)
        if calls["n"] < 3:  # ACM has not filled the record yet
            for option in got["Certificate"].get("DomainValidationOptions", []):
                option.pop("ResourceRecord", None)
        return got

    client.describe_certificate = late
    sleeps: list[float] = []
    monkeypatch.setattr(certificate.time, "sleep", sleeps.append)
    out = await provider.create(Context(), res)
    assert out["validation_name"] and out["validation_value"]
    assert len(sleeps) == 2


async def test_acm_create_raises_when_the_validation_record_never_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AwsProvider()
    res = AcmCertificate("c", domain_name="never.example.com")
    _handler, client = provider._dispatch(res, "create")
    client.describe_certificate = lambda **_kw: {"Certificate": {"DomainValidationOptions": []}}
    monkeypatch.setattr(certificate.time, "sleep", lambda _s: None)
    with pytest.raises(ProviderError, match="validation record"):
        await provider.create(Context(), res)


def test_acm_token_changes_with_the_requested_certificate() -> None:
    token = certificate._idempotency_token
    base = AcmCertificate("c", domain_name="a.example.com", subject_alternative_names=["b.x.com"])
    assert token(base) == token(
        AcmCertificate("c", domain_name="a.example.com", subject_alternative_names=["b.x.com"])
    )
    assert token(base) != token(AcmCertificate("c", domain_name="z.example.com"))
    assert token(base) != token(
        AcmCertificate("c", domain_name="a.example.com", subject_alternative_names=["c.x.com"])
    )
    assert token(base) != token(
        AcmCertificate(
            "c",
            domain_name="a.example.com",
            subject_alternative_names=["b.x.com"],
            validation_method="EMAIL",
        )
    )
    assert len(token(base)) == 32 and token(base).isalnum()


# -- 6. Route53 name normalisation --------------------------------------------------


class _Listing:
    def __init__(self, name: str) -> None:
        self.name = name

    def list_resource_record_sets(self, **_kw: Any) -> dict[str, Any]:
        return {"ResourceRecordSets": [{"Name": self.name, "Type": "A", "TTL": 60}]}


@pytest.mark.parametrize(
    ("declared", "live"),
    [("*.example.com", "\\052.example.com."), ("WWW.Example.com", "www.example.com.")],
)
def test_live_set_matches_escaped_and_lowercased_names(declared: str, live: str) -> None:
    res = Route53Record(
        "r", zone_id="Z1", record_name=declared, record_type="A", records=["1.2.3.4"]
    )
    assert Route53RecordHandler._live_set(_Listing(live), res) is not None


# -- 7. Hosted zone adopt matches the caller reference ------------------------------


async def test_zone_adopt_picks_the_zone_created_under_this_node() -> None:
    provider = AwsProvider()
    route53 = boto3.client("route53")
    # A same-named zone this node did not create, listed first.
    route53.create_hosted_zone(Name="example.org", CallerReference="someone-else")
    res = Route53HostedZone("z", domain="example.org")
    out = await provider.create(Context(), res)
    _handler, client = provider._dispatch(res, "create")

    def exists(**_kw: Any) -> None:
        raise client_error("HostedZoneAlreadyExists")

    client.create_hosted_zone = exists
    assert (await provider.create(Context(), res))["zone_id"] == out["zone_id"]


# -- 8. Adopt applies declared settings ---------------------------------------------


async def test_an_adopted_queue_takes_the_declared_attributes_and_tags() -> None:
    provider = AwsProvider()
    await provider.create(Context(), SqsQueue("q", queue_name="adopt-me", visibility_timeout=30))
    out = await provider.create(
        Context(),
        SqsQueue("q", queue_name="adopt-me", visibility_timeout=60, tags={"team": "ops"}),
    )
    sqs = boto3.client("sqs")
    attrs = sqs.get_queue_attributes(QueueUrl=out["url"], AttributeNames=["VisibilityTimeout"])
    assert attrs["Attributes"]["VisibilityTimeout"] == "60"
    assert sqs.list_queue_tags(QueueUrl=out["url"]).get("Tags", {}) == {"team": "ops"}


async def test_an_adopted_role_takes_the_declared_description_and_tags() -> None:
    provider = AwsProvider()
    await provider.create(
        Context(),
        IamRole("r", role_name="adopted", assume_role_policy=TRUST_POLICY, description="old"),
    )
    out = await provider.create(
        Context(),
        IamRole(
            "r",
            role_name="adopted",
            assume_role_policy=TRUST_POLICY,
            description="new",
            tags={"team": "ops"},
        ),
    )
    assert set(out) == {"arn"}
    role = boto3.client("iam").get_role(RoleName="adopted")["Role"]
    assert role["Description"] == "new"
    assert {t["Key"]: t["Value"] for t in role.get("Tags", [])} == {"team": "ops"}


# -- 9. SNS uses the stored ARN -----------------------------------------------------


async def test_sns_uses_the_stored_arn_without_listing_topics() -> None:
    provider = AwsProvider()
    arn = (await provider.create(Context(), SnsTopic("t", name="known")))["arn"]
    tracked = SnsTopic("t", name="known", arn=arn)
    _handler, client = provider._dispatch(tracked, "read")

    def no_listing(**_kw: Any) -> None:
        raise AssertionError("list_topics called despite a stored arn")

    client.list_topics = no_listing
    assert (await provider.read(Context(), tracked) or {})["arn"] == arn
    await provider.delete(Context(), tracked)
    assert await provider.read(Context(), tracked) is None
