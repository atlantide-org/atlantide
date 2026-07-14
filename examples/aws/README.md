# AWS example

Per-environment stacks (`dev`, `staging`, `prod`), each a small event pipeline:
an S3 bucket, an SQS queue, an SNS topic subscribed to the queue, a DynamoDB
table, a CloudWatch log group, a Lambda function (with its own role), a worker
IAM role + inline policy, a VPC with a subnet and security group, and a local
file recording the bucket's ARN. The engine orders every cross-resource and
cross-provider dependency automatically. 14 resources per env, 42 total.

The worker policy is built with the `allow()` helper (from
`atlantide.providers.aws`) rather than hand-written JSON, and each statement
targets a computed Ref (`assets.arn`, `jobs.arn`, `state.arn`, …) — so the policy
depends on those resources and the ARNs are resolved before it is written. The
VPC's `vpc_id` Ref likewise orders the subnet and security group after it.

- [`example-one.py`](example-one.py) — the Atlas-lang config.

## Feature showcase

[`example-two.py`](example-two.py) is a second, self-contained example
demonstrating three of the newer authoring features in one small graph:

- **Components** — `SecureBucket` is a library-authored L2 (bucket + a TLS-only
  Deny policy — no public grant, so it applies under S3 Block Public Access); one
  call expands to flat, namespaced nodes (`…:web-assets`, `…:web-policy`).
- **Per-block region** — the stack is `eu-north-1`, but a `logs` bucket sits
  inside `with region(Region.UsEast1):` and is created in `us-east-1` (its output
  domain resolves to `…s3.us-east-1.amazonaws.com`).
- **Output combinators** — `concat` / `interpolate` / `join` build outputs from
  apply-time refs (ARNs/domains unknown until apply), evaluated when the refs
  resolve.

```bash
cd examples/aws
uv run atlantide plan    example-two.py --state example-two.db
uv run atlantide apply   example-two.py --state example-two.db
uv run atlantide destroy --state example-two.db
```

## Static website

[`example-three.py`](example-three.py) is a third, self-contained example: a private
S3 origin bucket fronted by a CloudFront distribution through an Origin Access
Control (OAC), with a bucket policy that grants read access to **only that
distribution** (scoped by an `AWS:SourceArn` condition). The site is served from
the default `*.cloudfront.net` URL — no custom domain, ACM certificate, or Route53
records needed. Four resources, ordered by refs: `origin` + `oac` → `cdn` →
`origin-policy`.

Run it with a **separate** state db so it never touches `example-one.py`'s state (one
directory has one default `atlantide.toml`, so pass the config path explicitly):

```bash
cd examples/aws
uv run atlantide plan    example-three.py --state site.db
uv run atlantide apply   example-three.py --state site.db   # prints `bucket` + `site_url`
# upload content to the printed `bucket`, then open the `site_url` output:
aws s3 cp index.html s3://<bucket-from-outputs>/
uv run atlantide destroy --state site.db
```

> **CloudFront `destroy` is slow.** A distribution must be *disabled* and fully
> *redeployed* before it can be deleted; on real AWS that transition takes ~15-20
> minutes, so `destroy` blocks for that long. The provider disables it, polls until
> it reports `Deployed`, then deletes.

The `AcmCertificate` and `Route53HostedZone`/`Route53Record` types are also
supported by the provider (for custom-domain sites) but are not used by this
example.

## Run against AWS

The `atlantide` CLI wires the AWS provider, so with credentials in your
environment (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`,
or `AWS_PROFILE`):

```bash
cd examples/aws
# the processor Lambda references a secret by name — set it first (see Secrets)
uv run atlantide secret set app/signing-key-dev  --state infra.db
uv run atlantide secret set app/signing-key-prod --state infra.db

uv run atlantide plan    example-one.py --state infra.db          # preview the creates
uv run atlantide graph   example-one.py --format mermaid         # dependency graph
uv run atlantide apply   example-one.py --state infra.db --dry-run   # plan, change nothing
uv run atlantide apply   example-one.py --state infra.db         # create everything
uv run atlantide apply   example-one.py --state infra.db         # re-run: all unchanged (Merkle skip)
uv run atlantide destroy          --state infra.db             # tear everything down
```

(Or `source .venv/bin/activate` once, then drop the `uv run` prefix.)

The second apply performs **zero** AWS calls — unchanged nodes are skipped by the
Merkle input-hash.

## Secrets

The `processor` Lambda takes a `signing_secret` = `SecretRef("app/signing-key-<env>")`
— a *name*, never a value. The plaintext never appears in [`example-one.py`](example-one.py),
the IR, the `.atlas` artifact, or the state db; only the reference does. The value
is resolved from a local encrypted store at apply time, surfaced to the function as
its `SIGNING_SECRET` env var, and redacted (`(sensitive)`) in plan output.

Set the values out-of-band, once per env (omit the value to be prompted, no echo):

```bash
uv run atlantide secret set  app/signing-key-dev  --state infra.db
uv run atlantide secret list --state infra.db          # names only, never values
uv run atlantide secret get  app/signing-key-dev -r --state infra.db  # --reveal to print
uv run atlantide secret rm   app/signing-key-dev  --state infra.db
```

`plan`/`apply` fail fast if a referenced secret is undefined — no half-applied run:

```
error: undefined secret(s): prod:aws.LambdaFunction:processor.signing_secret -> 'app/signing-key-prod'
```

**Where it lives** (beside the state db — add both to `.gitignore`):

| file | contents |
|------|----------|
| `atlantide.secrets` | AES-256-GCM ciphertext of `{name: value}` |
| `atlantide.key`     | 32-byte key (`0600`, auto-generated on first `set`) |

**Rotation** — change a stored value (`secret set …` again) and the next `plan`
shows that node as an **update**; the value-free IR can't see the change otherwise,
so the engine compares a salted digest of the resolved value against state.

**Deploy** resolves from the *target* environment's store, so a built `.atlas`
carries no secrets — set the names on each environment. Relocate the store with
`secrets_store`/`secrets_key` in `atlantide.toml`, or resolve from the process
environment instead: `SecretRef("SIGNING_KEY", provider="env")`.

> This keyfile store is dev/CI-grade (encryption at rest, keeps secrets out of
> git/IR/state) — it is **not** a KMS: whoever can read both files reads the
> secret. Keep `atlantide.key` off shared disks and out of version control.

> Names are globally unique (S3 buckets) or account-unique (SQS/IAM) — change the
> `atlantide-*` names to something unique before applying for real. Editing
> `tags`/`versioning`/`description` updates in place; changing an immutable field
> (bucket/queue name, region) triggers a replace (destroy + create).
