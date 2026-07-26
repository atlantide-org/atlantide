# atlantide.providers.aws.handlers

One CRUD handler per resource type, grouped by service. `AwsProvider` dispatches
to them by type name; each owns its boto3 service client and its own identity
rules.

| Module | Handles |
| --- | --- |
| `s3.py` | Buckets, bucket policies, folder sync |
| `iam.py` | Roles and inline role policies |
| `compute.py` | Lambda functions |
| `database.py` | DynamoDB tables |
| `messaging.py` | SNS topics and subscriptions |
| `sqs.py` | Queues |
| `networking.py` | VPCs, subnets, security groups |
| `dns.py` | Route53 hosted zones and record sets |
| `certificate.py` | ACM certificates |
| `cloudfront.py` | Distributions and origin access controls |
| `observability.py` | CloudWatch log groups |
| `base.py` | The `AwsHandler` contract and the shared helpers below. |

Shared helpers in `base.py` exist because each is easy to get subtly wrong per
service:

- `is_missing` — only genuine not-found codes mean absence. A 403 or a throttle
  is not a missing resource, and refresh deletes the state row of anything it
  reads as missing.
- `create_or_adopt` — a create is re-run whenever its state row never reached
  `created`, so a name-keyed conflict adopts the existing resource instead of
  failing. EC2 has no name-based `get`, so `networking.py` adopts on a node tag
  rather than on attributes, which are not unique.
- `stale_tag_keys` / `tags_from_list` — AWS tagging APIs are additive, so
  removing a tag from config requires an explicit untag.
- `ignore_missing` — makes delete idempotent.
