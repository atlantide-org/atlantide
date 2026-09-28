"""Which types AWS can locate only by an opaque id, and on which field it lives.

``AwsHandler.identity_field`` is a *declaration*. Elsewhere,
:mod:`atlantide.reconcile.refresh` derives which fields a read observed from the
read's return value, because a declaration is a second source of truth that can
drift from what the handler does.

That argument does not apply here: the identity field is an **input to**
``read``, not an output of it. Nothing about a call reveals that ACM keys on an
arn and EC2 on a vpc id, so there is nothing to derive it from. Instead, these
tests hold the declaration to the handler, in the shape of
``test_read_coverage.py`` and with the same ratchet.
"""

from __future__ import annotations

import inspect

import pytest

from atlantide.core.fields import Mutability, field_mutability
from atlantide.providers.aws import AwsProvider, S3Bucket, Vpc
from atlantide.providers.aws.handlers import HANDLERS
from atlantide.providers.local import File, LocalProvider

#: type name -> the computed field carrying its provider-assigned id, or None
#: when ``read`` finds the resource from its declared attributes.
#:
#: The ``None`` entries are the types an import can adopt with no id, because
#: their read discovers them by name.
IDENTITY: dict[str, str | None] = {
    # Located by an opaque id.
    "aws.AcmCertificate": "arn",
    "aws.CloudFrontDistribution": "distribution_id",
    "aws.ElasticIp": "allocation_id",
    "aws.InternetGateway": "internet_gateway_id",
    "aws.NatGateway": "nat_gateway_id",
    "aws.OriginAccessControl": "oac_id",
    "aws.Route53HostedZone": "zone_id",
    "aws.RouteTable": "route_table_id",
    "aws.SecurityGroup": "group_id",
    "aws.Subnet": "subnet_id",
    "aws.Vpc": "vpc_id",
    # Located by name or by their declared attributes.
    "aws.AwsAvailabilityZones": None,
    "aws.AwsCallerIdentity": None,
    "aws.CloudWatchLogGroup": None,
    "aws.DynamoDbTable": None,
    "aws.IamPolicy": None,
    "aws.IamRole": None,
    "aws.LambdaFunction": None,
    "aws.Route53Record": None,
    "aws.S3Bucket": None,
    "aws.S3BucketPolicy": None,
    "aws.S3Folder": None,
    "aws.SnsSubscription": None,
    "aws.SnsTopic": None,
    "aws.SqsQueue": None,
}


def test_the_table_covers_every_registered_type() -> None:
    """Every new handler must declare one, so none needs an id nobody can supply."""
    assert set(IDENTITY) == set(HANDLERS), (
        "the identity table and the handler registry disagree — add the new type "
        "with the field its read needs restored (None if it finds itself by name)"
    )


@pytest.mark.parametrize("type_name", sorted(IDENTITY))
def test_the_declaration_matches_the_handler(type_name: str) -> None:
    assert HANDLERS[type_name].identity_field == IDENTITY[type_name]


@pytest.mark.parametrize("type_name", sorted(name for name, f in IDENTITY.items() if f))
def test_every_identity_field_is_a_real_computed_field(type_name: str) -> None:
    """It must be computed: an input field is stated by config, and a
    provider-assigned id is not. A typo would name a field nothing sets, and the
    read would fail for a misleading reason."""
    field = IDENTITY[type_name]
    assert field is not None  # narrowed by the parametrisation
    mutability = field_mutability(HANDLERS[type_name].resource_type)
    assert field in mutability, f"{type_name} has no field {field!r}"
    assert mutability[field] is Mutability.COMPUTED


@pytest.mark.parametrize("type_name", sorted(name for name, f in IDENTITY.items() if f))
def test_a_handler_using_known_id_declares_the_field_it_uses(type_name: str) -> None:
    """The drift guard.

    A handler that hard-codes ``known_id(res, "arn")`` would leave the declaration
    correct-looking but unused, and the two could diverge silently. Requiring the
    source to use the declaration keeps them in sync.
    """
    source = inspect.getsource(type(HANDLERS[type_name]))
    if "known_id(" not in source and "_known_id(" not in source:
        pytest.skip(f"{type_name}'s handler does not look up a recorded id")
    assert 'known_id(res, "' not in source, (
        f"{type_name}'s handler hard-codes an id field name — use self.identity_field "
        f"so the declaration and the lookup cannot drift apart"
    )


def test_the_provider_delegates_to_the_handler() -> None:
    """Callers outside ``providers`` use the provider-level accessor (nothing above
    this layer may import ``HANDLERS``), so it must report what the handler
    declares, including for a type it does not handle.

    It is keyed on the type, so a caller holding only a declaration need not build
    a resource (and resolve its refs and secrets) to ask.
    """
    provider = AwsProvider()
    assert provider.identity_field(Vpc) == "vpc_id"
    assert provider.identity_field(S3Bucket) is None
    assert provider.identity_field(File) is None


def test_a_provider_that_declares_nothing_still_answers() -> None:
    """The base implementation is not abstract, so a provider whose resources are
    all name-addressed implements nothing and still works."""
    assert LocalProvider().identity_field(File) is None
