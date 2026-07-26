# atlantide.providers.aws.resources

The declarable AWS resource types — what a config actually writes. One module
per service, re-exported flat from the package.

Each class declares its fields as `immutable()`, `mutable()`, `computed()`, or
`secret()`; that classification is what decides UPDATE versus REPLACE at diff
time, so it is part of the type's contract rather than an implementation detail.

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
A type that inherits neither is genuinely global — IAM, CloudFront, Route53 —
and the absence of a `region` on it is a fact worth being able to see.

A resource whose cloud name config chooses marks that field `physical_name=True`,
which is what lets a `Stack`'s `name_prefix` compose one when config omits it and
what the planner treats as the resource's identity when replacing it.
`tests/providers/test_physical_names.py` holds every type to that declaration.
