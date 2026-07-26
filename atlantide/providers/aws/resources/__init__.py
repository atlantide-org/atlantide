"""AWS resource types, one module per service, re-exported flat here.

Each service module owns its resources and their plan-time validators.
"""

from atlantide.providers.aws.resources.base import (
    AwsResource,
    Ec2Resource,
    RegionalResource,
    TaggedResource,
)
from atlantide.providers.aws.resources.certificate import AcmCertificate
from atlantide.providers.aws.resources.cloudfront import (
    CloudFrontDistribution,
    OriginAccessControl,
)
from atlantide.providers.aws.resources.compute import LambdaFunction
from atlantide.providers.aws.resources.data import (
    AwsAvailabilityZones,
    AwsCallerIdentity,
    AwsDataSource,
)
from atlantide.providers.aws.resources.database import DynamoDbTable
from atlantide.providers.aws.resources.dns import Route53HostedZone, Route53Record
from atlantide.providers.aws.resources.iam import IamPolicy, IamRole
from atlantide.providers.aws.resources.messaging import SnsSubscription, SnsTopic
from atlantide.providers.aws.resources.networking import (
    ALLOW_ALL_EGRESS,
    ElasticIp,
    InternetGateway,
    NatGateway,
    Route,
    RouteTable,
    SecurityGroup,
    SgRule,
    Subnet,
    Vpc,
)
from atlantide.providers.aws.resources.observability import CloudWatchLogGroup
from atlantide.providers.aws.resources.s3 import S3Bucket, S3BucketPolicy, S3Folder
from atlantide.providers.aws.resources.sqs import SqsQueue

__all__ = [
    "ALLOW_ALL_EGRESS",
    "AcmCertificate",
    "AwsAvailabilityZones",
    "AwsCallerIdentity",
    "AwsDataSource",
    "AwsResource",
    "CloudFrontDistribution",
    "CloudWatchLogGroup",
    "DynamoDbTable",
    "Ec2Resource",
    "ElasticIp",
    "IamPolicy",
    "IamRole",
    "InternetGateway",
    "LambdaFunction",
    "NatGateway",
    "OriginAccessControl",
    "RegionalResource",
    "Route",
    "Route53HostedZone",
    "Route53Record",
    "RouteTable",
    "S3Bucket",
    "S3BucketPolicy",
    "S3Folder",
    "SecurityGroup",
    "SgRule",
    "SnsSubscription",
    "SnsTopic",
    "SqsQueue",
    "Subnet",
    "TaggedResource",
    "Vpc",
]
