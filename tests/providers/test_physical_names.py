"""Which field carries each type's cloud name, and what a stack composes into it.

``physical_name=True`` is the second declaration a resource makes about its
identity (the first being the handler's ``identity_field``, held to the handler
by ``test_identity_fields.py``). It drives two unrelated things:

* a ``Stack`` with a ``name_prefix`` composes ``{prefix}-{logical}-{stack}`` into
  the field when config omitted it, so names do not collide across stacks;
* the planner treats the field as the resource's identity when deciding whether
  a create-before-destroy replacement would collide with the resource it replaces.

Neither is derivable from the type — a cloud name is not distinguishable from any
other required string by shape — so this table is what holds the declarations
still, in the shape of ``test_identity_fields.py`` and for the same reason.
"""

from __future__ import annotations

import pytest

from atlantide.core.fields import Mutability, field_mutability, physical_name_field
from atlantide.core.stack import Stack
from atlantide.providers.aws import TYPES

#: type name -> the field holding its cloud name, or None when it has none.
#:
#: ``None`` covers three shapes, none of them an oversight: a type AWS locates by
#: an opaque id it assigns (every EC2 resource, CloudFront's distribution), one
#: whose identity is another resource's name (``S3Folder``, ``S3BucketPolicy``,
#: ``IamPolicy``, ``SnsSubscription``), and the read-only lookups, which name
#: nothing because they create nothing.
PHYSICAL_NAMES: dict[str, str | None] = {
    # Named: config chooses the name AWS will use.
    "aws.AcmCertificate": "domain_name",
    "aws.CloudWatchLogGroup": "log_group_name",
    "aws.DynamoDbTable": "table_name",
    "aws.IamRole": "role_name",
    "aws.LambdaFunction": "function_name",
    "aws.OriginAccessControl": "oac_name",
    "aws.Route53HostedZone": "domain",
    "aws.S3Bucket": "bucket",
    "aws.SecurityGroup": "group_name",
    "aws.SnsTopic": "name",
    "aws.SqsQueue": "queue_name",
    # Unnamed: located by an id AWS assigns, by another resource's name, or
    # creating nothing at all.
    "aws.AwsAvailabilityZones": None,
    "aws.AwsCallerIdentity": None,
    "aws.CloudFrontDistribution": None,
    "aws.ElasticIp": None,
    "aws.IamPolicy": None,
    "aws.InternetGateway": None,
    "aws.NatGateway": None,
    "aws.Route53Record": None,
    "aws.RouteTable": None,
    "aws.S3BucketPolicy": None,
    "aws.S3Folder": None,
    "aws.SnsSubscription": None,
    "aws.Subnet": None,
    "aws.Vpc": None,
}

#: The named types whose name is a *domain*, which a ``name_prefix`` cannot
#: compose: ``acme-main-prod`` is a fine resource name and not a domain at all.
#: These declare ``physical_name`` for the planner's sake and validate the field
#: so the composed value cannot reach AWS.
DOMAIN_NAMED = {"aws.AcmCertificate", "aws.Route53HostedZone"}

#: Inputs a type needs beyond its required fields, because a model validator
#: demands them. Only for types whose rule spans two optional fields, which
#: ``_required_placeholders`` cannot infer.
EXTRA_INPUTS: dict[str, dict[str, str]] = {
    "aws.IamRole": {"assumed_by": "lambda.amazonaws.com"},
}


def test_the_table_covers_every_registered_type() -> None:
    """A new type has to decide, rather than defaulting to unnamed by silence.

    Defaulting is what this guards: a named resource that forgets the marker
    still works, right up until someone sets a ``name_prefix`` and finds that one
    type ignores it — which is how ``CloudWatchLogGroup`` shipped without it.
    """
    assert set(PHYSICAL_NAMES) == set(TYPES), (
        "the physical-name table and the AWS type registry disagree — add the new "
        "type with the field holding its cloud name (None if AWS assigns its id, "
        "or if it takes its identity from another resource)"
    )


@pytest.mark.parametrize("type_name", sorted(PHYSICAL_NAMES))
def test_the_declaration_matches_the_type(type_name: str) -> None:
    assert physical_name_field(TYPES[type_name]) == PHYSICAL_NAMES[type_name]


@pytest.mark.parametrize("type_name", sorted(name for name, f in PHYSICAL_NAMES.items() if f))
def test_a_cloud_name_is_an_immutable_input(type_name: str) -> None:
    """Immutable because AWS will not rename in place: a new name is a new
    resource. A ``mutable`` cloud name would plan an UPDATE for a change only a
    REPLACE can deliver, and the apply would fail at the provider."""
    field = PHYSICAL_NAMES[type_name]
    assert field is not None  # narrowed by the parametrisation
    mutability = field_mutability(TYPES[type_name])
    assert field in mutability, f"{type_name} has no field {field!r}"
    assert mutability[field] is Mutability.IMMUTABLE


@pytest.mark.parametrize(
    "type_name",
    sorted(name for name, f in PHYSICAL_NAMES.items() if f and name not in DOMAIN_NAMED),
)
def test_a_named_type_composes_its_name_under_a_prefix(type_name: str) -> None:
    """The behaviour the marker exists for, exercised through a real stack.

    Asserting the composition rather than the marker: a caller cares that two
    stacks built from one config do not fight over a name, and that is only true
    if the field is actually filled.
    """
    field = PHYSICAL_NAMES[type_name]
    assert field is not None  # narrowed by the parametrisation
    with Stack("prod", region="us-east-1", name_prefix="acme"):
        resource = TYPES[type_name](  # every other required field is region-or-ref shaped
            "thing", **_required_placeholders(type_name, skip=field)
        )
    assert getattr(resource, field) == "acme-thing-prod"


@pytest.mark.parametrize("type_name", sorted(DOMAIN_NAMED))
def test_a_domain_named_type_refuses_a_composed_name(type_name: str) -> None:
    """The composed name is not a domain, and the type says so at plan time.

    Left unvalidated this is the worst kind of failure: nobody typed the value, so
    the AWS error names a string the config does not contain.
    """
    # The name field is omitted, exactly as in the case this guards: that is what
    # makes the stack compose one, which is the value under test.
    with (
        pytest.raises(ValueError, match="expected a dotted name"),
        Stack("prod", region="us-east-1", name_prefix="acme"),
    ):
        TYPES[type_name](
            "thing", **_required_placeholders(type_name, skip=PHYSICAL_NAMES[type_name])
        )


def _required_placeholders(type_name: str, *, skip: str | None) -> dict[str, str]:
    """Fill every required input except ``skip`` (and ``region``, which the stack
    supplies) with a string, so the type can be built to observe one field."""
    cls = TYPES[type_name]
    mutability = field_mutability(cls)
    filled = {
        name: "x"
        for name, info in cls.model_fields.items()
        if info.is_required()
        and name not in (skip, "region")
        and mutability.get(name) is not Mutability.COMPUTED
    }
    return filled | EXTRA_INPUTS.get(type_name, {})
