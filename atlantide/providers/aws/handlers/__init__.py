"""Per-resource CRUD handlers for the AWS provider, one module per service.

:data:`HANDLERS` is the single registration point: adding a handler here also
registers its resource type (``atlantide.providers.aws.TYPES`` derives from it).
"""

from __future__ import annotations

from typing import Any

from atlantide.providers.aws.handlers.base import AwsHandler
from atlantide.providers.aws.handlers.certificate import AcmCertificateHandler
from atlantide.providers.aws.handlers.cloudfront import (
    CloudFrontDistributionHandler,
    CloudFrontOacHandler,
)
from atlantide.providers.aws.handlers.compute import LambdaFunctionHandler
from atlantide.providers.aws.handlers.database import DynamoDbTableHandler
from atlantide.providers.aws.handlers.dns import (
    Route53HostedZoneHandler,
    Route53RecordHandler,
)
from atlantide.providers.aws.handlers.iam import IamPolicyHandler, IamRoleHandler
from atlantide.providers.aws.handlers.messaging import SnsSubscriptionHandler, SnsTopicHandler
from atlantide.providers.aws.handlers.networking import (
    SecurityGroupHandler,
    SubnetHandler,
    VpcHandler,
)
from atlantide.providers.aws.handlers.observability import CloudWatchLogGroupHandler
from atlantide.providers.aws.handlers.s3 import S3BucketHandler, S3BucketPolicyHandler
from atlantide.providers.aws.handlers.sqs import SqsQueueHandler

__all__ = ["HANDLERS", "AwsHandler"]

_HANDLER_CLASSES: list[type[AwsHandler[Any]]] = [
    S3BucketHandler,
    S3BucketPolicyHandler,
    SqsQueueHandler,
    IamRoleHandler,
    IamPolicyHandler,
    LambdaFunctionHandler,
    SnsTopicHandler,
    SnsSubscriptionHandler,
    DynamoDbTableHandler,
    CloudWatchLogGroupHandler,
    VpcHandler,
    SubnetHandler,
    SecurityGroupHandler,
    CloudFrontOacHandler,
    CloudFrontDistributionHandler,
    AcmCertificateHandler,
    Route53HostedZoneHandler,
    Route53RecordHandler,
]

#: Resource ``type_name`` -> handler instance.
HANDLERS: dict[str, AwsHandler[Any]] = {
    handler_cls.resource_type.type_name(): handler_cls() for handler_cls in _HANDLER_CLASSES
}
