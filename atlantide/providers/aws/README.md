# atlantide.providers.aws

The AWS provider: resource types, the boto3-backed CRUD dispatcher, and the
helpers shared across services.

| Module | Purpose |
| --- | --- |
| `provider.py` | `AwsProvider` — dispatches to a per-resource handler, manages boto3 clients per region and credential alias, and retries transient failures. |
| `resources/` | The declarable resource types, one module per service. |
| `handlers/` | The CRUD implementation for each of those types. |
| `components.py` | L2 components built from the L1 resources (for example `SecureBucket`). |
| `policy.py` | Builders for IAM policy statements: `allow`, `deny`, `assume_role`. |
| `region.py` | Region constants. |
| `config.py` | botocore client tuning: timeouts, connection pool, transport retries. |
| `validate.py` | Composable, resource-agnostic input validators used by the resource types. |

Handlers are synchronous (boto3 is) and run in a worker thread. Clients are
typed `handlers.base.Client`, an alias of `Any`, to avoid a hard dependency on
per-service type stubs.
