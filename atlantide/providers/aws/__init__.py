"""atlantide.providers.aws: AWS provider and resource types."""

from collections.abc import Mapping
from typing import Any

from atlantide.core.plugin import ProviderPlugin
from atlantide.core.resource import Resource
from atlantide.providers.aws.components import SecureBucket
from atlantide.providers.aws.handlers import HANDLERS
from atlantide.providers.aws.policy import ServicePrincipal, allow, assume_role, deny
from atlantide.providers.aws.provider import AwsAlias, AwsProvider
from atlantide.providers.aws.region import Region
from atlantide.providers.aws.resources import (
    ALLOW_ALL_EGRESS,
    AcmCertificate,
    AwsAvailabilityZones,
    AwsCallerIdentity,
    CloudFrontDistribution,
    CloudWatchLogGroup,
    DynamoDbTable,
    ElasticIp,
    IamPolicy,
    IamRole,
    InternetGateway,
    LambdaFunction,
    NatGateway,
    OriginAccessControl,
    Route,
    Route53HostedZone,
    Route53Record,
    RouteTable,
    S3Bucket,
    S3BucketPolicy,
    S3Folder,
    SecurityGroup,
    SgRule,
    SnsSubscription,
    SnsTopic,
    SqsQueue,
    Subnet,
    Vpc,
)

#: Resource types this provider manages, keyed by ``type_name``.
#: Derived from the handler registry so a type cannot exist without CRUD.
TYPES: dict[str, type[Resource]] = {
    name: handler.resource_type for name, handler in HANDLERS.items()
}


def _build(settings: Mapping[str, Any]) -> AwsProvider:
    """Construct the AWS provider from its settings table.

    A raw mapping rather than typed parameters, because that is the contract a
    third-party provider gets: it may accept keys this codebase has never heard
    of. Unknown keys are ignored rather than rejected, so a config written for a
    newer provider still loads against an older one.
    """
    aliases = {
        name: AwsAlias(profile=cfg.get("profile"), endpoint_url=cfg.get("endpoint"))
        for name, cfg in (settings.get("aliases") or {}).items()
    }
    kwargs: dict[str, Any] = {"aliases": aliases} if aliases else {}
    for key, argument in (
        ("region", "region"),
        ("profile", "profile"),
        ("endpoint", "endpoint_url"),
        ("parallelism", "parallelism"),
    ):
        if (value := settings.get(key)) is not None:
            kwargs[argument] = value
    return AwsProvider(**kwargs)


#: See :mod:`atlantide.core.plugin`.
PLUGIN = ProviderPlugin(
    name=AwsProvider.name,
    types=TYPES,
    factory=_build,
    module="atlantide.providers.aws",
    summary="AWS resources over boto3.",
)

__all__ = [
    "ALLOW_ALL_EGRESS",
    "PLUGIN",
    "TYPES",
    "AcmCertificate",
    "AwsAlias",
    "AwsAvailabilityZones",
    "AwsCallerIdentity",
    "AwsProvider",
    "CloudFrontDistribution",
    "CloudWatchLogGroup",
    "DynamoDbTable",
    "ElasticIp",
    "IamPolicy",
    "IamRole",
    "InternetGateway",
    "LambdaFunction",
    "NatGateway",
    "OriginAccessControl",
    "Region",
    "Route",
    "Route53HostedZone",
    "Route53Record",
    "RouteTable",
    "S3Bucket",
    "S3BucketPolicy",
    "S3Folder",
    "SecureBucket",
    "SecurityGroup",
    "ServicePrincipal",
    "SgRule",
    "SnsSubscription",
    "SnsTopic",
    "SqsQueue",
    "Subnet",
    "Vpc",
    "allow",
    "assume_role",
    "deny",
]
