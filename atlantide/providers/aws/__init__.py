"""atlantide.providers.aws: AWS provider and resource types."""

from atlantide.core.resource import Resource
from atlantide.providers.aws.components import SecureBucket
from atlantide.providers.aws.handlers import HANDLERS
from atlantide.providers.aws.policy import ServicePrincipal, allow, assume_role, deny
from atlantide.providers.aws.provider import AwsAlias, AwsProvider
from atlantide.providers.aws.region import Region
from atlantide.providers.aws.resources import (
    AcmCertificate,
    CloudFrontDistribution,
    CloudWatchLogGroup,
    DynamoDbTable,
    IamPolicy,
    IamRole,
    LambdaFunction,
    OriginAccessControl,
    Route53HostedZone,
    Route53Record,
    S3Bucket,
    S3BucketPolicy,
    S3Folder,
    SecurityGroup,
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

__all__ = [
    "TYPES",
    "AcmCertificate",
    "AwsAlias",
    "AwsProvider",
    "CloudFrontDistribution",
    "CloudWatchLogGroup",
    "DynamoDbTable",
    "IamPolicy",
    "IamRole",
    "LambdaFunction",
    "OriginAccessControl",
    "Region",
    "Route53HostedZone",
    "Route53Record",
    "S3Bucket",
    "S3BucketPolicy",
    "S3Folder",
    "SecureBucket",
    "SecurityGroup",
    "ServicePrincipal",
    "SnsSubscription",
    "SnsTopic",
    "SqsQueue",
    "Subnet",
    "Vpc",
    "allow",
    "assume_role",
    "deny",
]
