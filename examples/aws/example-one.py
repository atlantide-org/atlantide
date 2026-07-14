"""Small Atlantide AWS example: per-environment stacks + cross-resource refs.

Valid Python syntax (formatters/linters work), but run by the Atlas-lang
interpreter, so it is deterministic by construction — no clock, randomness, env,
or network access. A few names (``uuid5``, ``sha256_hex``, ``b64encode``) are
Atlas-lang *builtins* — pure derived functions the interpreter injects, so they
are used without an import (a static checker will flag them as undefined).

Things to notice:

- **Stacks** — each environment is its own :class:`Stack`. The stack carries
  defaults (``region``, ``name_prefix``, ``tags``) that every resource inside
  inherits, so ``S3Bucket("assets")`` in the ``dev`` stack becomes the bucket
  ``atlantide-assets-dev`` with the stack's tags. The same logical names live in
  every stack without colliding (node ids are ``dev:aws.S3Bucket:assets`` etc.).
- **Refs** — reading another resource's computed output (``bucket.arn``) returns
  a lazy reference. That both wires the dependency (the engine orders the ref'd
  resource first) and resolves to the real value at apply time.
- **Secrets** — the Lambda's ``signing_secret`` holds a ``SecretRef`` — a *name*,
  never a value. The config source, the IR, and the state file contain only the
  reference; the plaintext is resolved from the secrets backend at apply and shows
  as ``(sensitive)`` in plan output. Populate it out-of-band, once per
  environment: ``atlantide secret set app/signing-key-<env> <value>``.
- **Mixed providers** — ``random.Id`` is a resource, not a config-time function:
  its value is generated once at apply and pinned in state (re-apply is a NOOP,
  never a new value). The AWS bucket references it (``build.result``), so the
  engine orders the ``random`` resource first — one graph spanning two providers.
- **Derived values** — ``uuid5``/``sha256_hex`` are pure Atlas-lang builtins,
  evaluated at config time (their result is a fixed value in the IR). Here they
  give the bucket a deterministic, globally-unique name and a config-digest tag —
  contrast ``random.Id``, whose value is decided at *apply*, not config, time.
"""

from atlantide.core import SecretRef, Stack, output
from atlantide.policy import enforce
from atlantide.providers.aws import (
    IamPolicy,
    IamRole,
    LambdaFunction,
    Region,
    S3Bucket,
    SecurityGroup,
    ServicePrincipal,
    SqsQueue,
    Vpc,
    allow,
)
from atlantide.providers.random import Id

enforce("require-tags")  # plan-time policy: every taggable resource must carry tags

# A shared `common` stack owning one VPC that every environment builds on. Its
# `vpc_id` output is read cross-stack by dev/prod (below) rather than each defining
# a VPC of their own. Because `common` lives in *this* config, the engine inlines
# that reference into a real dependency edge and applies `common` before dev/prod
# in a single run (dev and prod, being independent, apply in parallel).
with Stack("common", region=Region.EuNorth1, name_prefix="atlantide", tags={"env": "common"}):
    network = Vpc("network", cidr_block="10.0.0.0/16")
    # `output()` returns a typed handle to the export. dev/prod below consume this
    # variable directly, so the output name lives in exactly one place (a typo is an
    # undefined-variable error, not a plan-time string mismatch).
    vpc_id = output("vpc_id", network.vpc_id)  # computed VPC id — consumed by dev + prod

for env in ["dev", "prod"]:
    # region + name_prefix + tags are stack-scoped: resources inside inherit them.
    with Stack(env, region=Region.EuNorth1, name_prefix="atlantide", tags={"env": env}):
        # A random build id, generated once at apply and pinned in state. The
        # bucket tags it (a Ref), so the engine creates this random resource before
        # the AWS bucket — a single graph across the random + aws providers.
        build = Id("build-id", byte_length=4)

        # `uuid5`/`sha256_hex` are Atlas-lang builtins (deterministic, config-time).
        # Derive a stable, globally-unique bucket name + a config-digest tag; both
        # are fixed values baked into the IR.
        bucket_name = f"atlantide-assets-{env}-{uuid5('atlantide-buckets', env)[:8]}"  # noqa: F821
        assets = S3Bucket(
            "assets",
            bucket=bucket_name,
            versioning=(env == "prod"),
            tags={"build_id": build.result, "config_hash": sha256_hex(env)[:12]},  # noqa: F821
        )
        jobs = SqsQueue("jobs", fifo=True)

        # The shared VPC lives in the `common` stack. Consuming its `vpc_id` handle
        # is inlined to a real ref on `common`'s VPC — a within-graph edge that orders
        # `common` first automatically. (A stack applied by a *separate* config would
        # instead name it via `StackReference("common").output("vpc_id")`, resolved
        # from committed state.) Each env hangs its own security group off the shared VPC.
        edge = SecurityGroup("edge", group_name=f"edge-{env}", vpc_id=vpc_id)

        # A worker role EC2 and Lambda can assume (the processor below uses it);
        # the provider builds the trust document from these principals.
        worker = IamRole("worker", assumed_by=[ServicePrincipal.Ec2, ServicePrincipal.Lambda])

        # A processor Lambda assuming the worker role. `signing_secret` is a
        # SecretRef: it names the secret, and the value is resolved from the
        # secrets store at apply — never present in this file, the IR, or state.
        processor = LambdaFunction(
            "processor",
            role_arn=worker.arn,
            handler="app.handler",
            signing_secret=SecretRef(f"app/signing-key-{env}"),
        )

        # Inline policy granting the worker access to the bucket and queue. Each
        # `on=` is a computed Ref, so the policy depends on (and applies after)
        # the role, the bucket and the queue.
        IamPolicy(
            "worker-policy",
            role_arn=worker.arn,
            policy_name="worker-access",
            statements=[
                allow(S3Bucket.Action.GetObject, S3Bucket.Action.PutObject, on=assets.objects_arn),
                allow(SqsQueue.Action.SendMessage, on=jobs.arn),
            ],
        )

        # Exported per stack — the CLI prints them after apply. Values are computed
        # Refs (resolved at apply) or plain literals; both show up under Outputs.
        output("assets_arn", assets.arn)          # computed bucket ARN
        output("assets_bucket", assets.bucket)    # the resolved bucket name
        output("jobs_url", jobs.url)              # computed queue URL
        output("jobs_arn", jobs.arn)              # computed queue ARN
        output("worker_role_arn", worker.arn)    # computed IAM role ARN
        output("processor_arn", processor.arn)   # computed Lambda ARN
        output("build_id", build.result)         # the pinned random id
        output("edge_sg_id", edge.group_id)      # computed SG id (on the shared VPC)
        output("region", Region.EuNorth1)        # a literal
