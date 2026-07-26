"""Which types AWS can only locate by an opaque id, and on which field it lives.

``AwsHandler.identity_field`` is a *declaration*, and this repo is otherwise
sceptical of those: :mod:`atlantide.reconcile.refresh` deliberately derives which
fields a read observed from the read's own return value, on the grounds that a
declaration is a second source of truth that drifts from what the handler does.

That objection does not transfer, for one reason: the identity field is an
**input to** ``read``, not an output of it. Nothing about a call tells you that
ACM keys on an arn and EC2 on a vpc id, so there is nothing to derive it from.
What can be done instead is to hold the declaration to the handler by test, which
is what this file is — built in the shape of ``test_read_coverage.py``, for the
same reason and with the same ratchet.
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
#: The ``None`` entries are not a lesser case: they are the types an import can
#: adopt with no id at all, because their read already discovers them by name.
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
    """A new handler has to decide, so one cannot arrive with an id nobody can supply."""
    assert set(IDENTITY) == set(HANDLERS), (
        "the identity table and the handler registry disagree — add the new type "
        "with the field its read needs restored (None if it finds itself by name)"
    )


@pytest.mark.parametrize("type_name", sorted(IDENTITY))
def test_the_declaration_matches_the_handler(type_name: str) -> None:
    assert HANDLERS[type_name].identity_field == IDENTITY[type_name]


@pytest.mark.parametrize("type_name", sorted(name for name, f in IDENTITY.items() if f))
def test_every_identity_field_is_a_real_computed_field(type_name: str) -> None:
    """It has to be computed: an input field is something config states, and a
    provider-assigned id is by definition not. A typo here would name a field
    nothing ever sets, and the read would go on failing for a different reason."""
    field = IDENTITY[type_name]
    assert field is not None  # narrowed by the parametrisation
    mutability = field_mutability(HANDLERS[type_name].resource_type)
    assert field in mutability, f"{type_name} has no field {field!r}"
    assert mutability[field] is Mutability.COMPUTED


@pytest.mark.parametrize("type_name", sorted(name for name, f in IDENTITY.items() if f))
def test_a_handler_using_known_id_declares_the_field_it_uses(type_name: str) -> None:
    """The drift guard.

    A handler that goes back to a hard-coded ``known_id(res, "arn")`` would leave
    the declaration correct-looking and unused, and the two could then diverge
    silently. Requiring the declaration to be what the source reaches for is what
    keeps them the same thing.
    """
    source = inspect.getsource(type(HANDLERS[type_name]))
    if "known_id(" not in source and "_known_id(" not in source:
        pytest.skip(f"{type_name}'s handler does not look up a recorded id")
    assert 'known_id(res, "' not in source, (
        f"{type_name}'s handler hard-codes an id field name — use self.identity_field "
        f"so the declaration and the lookup cannot drift apart"
    )


def test_the_provider_delegates_to_the_handler() -> None:
    """The provider-level accessor is what a caller outside ``providers`` reaches
    for — nothing above this layer may import ``HANDLERS`` — so it has to report
    what the handler declares, including for a type it has never heard of.

    Keyed on the type, so a caller holding only a declaration never has to build a
    resource (and resolve its refs and secrets) to ask.
    """
    provider = AwsProvider()
    assert provider.identity_field(Vpc) == "vpc_id"
    assert provider.identity_field(S3Bucket) is None
    assert provider.identity_field(File) is None


def test_a_provider_that_declares_nothing_still_answers() -> None:
    """The base implementation is not abstract, so a provider whose resources are
    all name-addressed implements nothing and still works."""
    assert LocalProvider().identity_field(File) is None
