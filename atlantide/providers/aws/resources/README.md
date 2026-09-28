# atlantide.providers.aws.resources

The AWS resource types a config declares, one module per service, re-exported flat
from the package.

Each class declares its fields as `immutable()`, `mutable()`, `computed()`, or
`secret()`. The classification decides UPDATE versus REPLACE at diff time, so it is
part of the type's contract.

| Module | Types |
| --- | --- |
| `s3.py` | `S3Bucket`, `S3BucketPolicy`, `S3Folder` |
| `iam.py` | `IamRole`, `IamPolicy` |
| `compute.py` | `LambdaFunction` |
| `database.py` | `DynamoDbTable` |
| `messaging.py` | `SnsTopic`, `SnsSubscription` |
| `sqs.py` | `SqsQueue` |
| `networking.py` | `Vpc`, `Subnet`, `SecurityGroup`, `InternetGateway`, `NatGateway`, `ElasticIp`, `RouteTable`, and the `SgRule` / `Route` nested types |
| `dns.py` | `Route53HostedZone`, `Route53Record`, `AliasTarget` |
| `certificate.py` | `AcmCertificate` (DNS validation) |
| `cloudfront.py` | `CloudFrontDistribution`, `OriginAccessControl` |
| `observability.py` | `CloudWatchLogGroup` |
| `data.py` | Read-only lookups: `AwsCallerIdentity`, `AwsAvailabilityZones` |
| `base.py` | `AwsResource` (provider tag, `provider_alias`), `RegionalResource` (`region`), `TaggedResource` (`tags`), `Ec2Resource` (both). |

Regional and tagged are separate bases because the two do not coincide: a global
ACM certificate is tagged, and an SNS subscription is regional and untaggable.
A type that does not inherit `RegionalResource` (IAM, CloudFront, Route53, ACM) is
global and has no `region` field.

A resource whose cloud name the config chooses marks that field `physical_name=True`.
A `Stack`'s `name_prefix` composes that name when config omits it, and the planner
treats it as the resource's identity when replacing it.
`tests/providers/aws/test_physical_names.py` checks every type against that
declaration.
