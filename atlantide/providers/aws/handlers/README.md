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
| `networking.py` | VPCs, subnets, security groups, internet and NAT gateways, elastic IPs, route tables |
| `ec2.py` | `Ec2Handler`, the shared base for EC2 types located by id and adopted by node tag |
| `sgrules.py` | Translation between a declared `SgRule` and EC2's `IpPermission` |
| `data.py` | Read-only lookups (data sources): caller identity, availability zones |
| `dns.py` | Route53 hosted zones and record sets |
| `certificate.py` | ACM certificates |
| `cloudfront.py` | Distributions and origin access controls |
| `observability.py` | CloudWatch log groups |
| `base.py` | The `AwsHandler` contract, the `Client` alias, and the shared helpers below. |
| `faults.py` | Error classification (`is_missing`, `ignore_missing`) and `create_or_adopt` |
| `tags.py` | Tag translation and syncing |
| `pagination.py` | The `NextToken` and `Marker` listing protocols |

The shared helpers, all re-exported from `base.py` so a handler has one import
site, exist because each is easy to get subtly wrong per service:

- `is_missing` — only genuine not-found codes mean absence. A 403 or a throttle
  is not a missing resource, and refresh deletes the state row of anything it
  reads as missing.
- `create_or_adopt` — a create is re-run whenever its state row never reached
  `created`, so a name-keyed conflict adopts the existing resource instead of
  failing. EC2 has no name-based `get`, so `Ec2Handler` (`ec2.py`) adopts on a node
  tag rather than on attributes, which are not unique.
- `stale_tag_keys` / `tags_from_list` — AWS tagging APIs are additive, so
  removing a tag from config requires an explicit untag.
- `ignore_missing` — makes delete idempotent.
