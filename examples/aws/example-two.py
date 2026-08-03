"""Atlantide showcase: an L2 component, a per-block region override, and output
combinators — three of the newer authoring features in one small graph.

Valid Python syntax, but run by the deterministic Atlas-lang interpreter (no
clock, randomness, env, or network at config time). ``uuid5`` is an Atlas-lang
builtin (a pure derived function the interpreter injects); ``concat`` /
``interpolate`` / ``join`` / ``region`` are imported from ``atlantide.core`` so the
file stays clean Python.

What it demonstrates:

- **Config** — one :class:`Config` declares both environments and what differs
  between them, as an :class:`EnvSchema` subclass, so ``env.log_region``
  completes in an editor and a misspelling is a type error. The per-block region
  override below reads ``log_region``, so where the logs live is a declared
  property of the environment rather than a literal in the body. ``atlantide plan
  --env prod`` narrows a run to one environment.
- **Components** — ``SecureBucket`` is a library-authored L2 (an S3 bucket plus a
  TLS-only Deny bucket policy — no public grant, so it applies under S3 Block
  Public Access). Instantiating it expands to flat, namespaced nodes
  (``dev:aws.S3Bucket:web-assets`` + ``...:web-policy``); a second instance
  never collides. The component exposes ``.bucket`` / ``.arn`` / ``.domain_name``.
- **Per-block region** — the stack's region comes from the environment, but the
  ``logs`` bucket sits inside ``with region(env.log_region):`` so it is created
  wherever that says. In ``prod`` that is ``us-east-1``, a central log archive
  away from the stack's ``eu-north-1``; ``dev`` keeps its logs local. The
  override restores on exit — resources after it are back in the stack's region.
- **Output combinators** — ``concat`` / ``interpolate`` / ``join`` build values
  *from* apply-time refs (bucket ARNs / domains that aren't known until apply).
  They serialize as data and evaluate once the refs resolve, so the graph and its
  content hash stay deterministic.

Run it (keep it off the other examples' state with a separate db):

    cd examples/aws
    uv run atlantide plan    example-two.py --state example-two.db
    uv run atlantide apply   example-two.py --state example-two.db
    uv run atlantide apply   example-two.py --state example-two.db --env prod  # one env
    uv run atlantide destroy --state example-two.db
"""

from atlantide.core import Config, EnvSchema, Stack, concat, interpolate, join, output, region
from atlantide.providers.aws import Region, S3Bucket, SecureBucket


class ShowcaseEnv(EnvSchema):
    """What differs between the environments below.

    A body of annotated fields — the one class Atlas-lang admits. Declaring it
    makes `env.log_region` complete in an editor rather than resolve to `Any`.
    """

    #: Where this environment's logs go. Required: the region override below has
    #: no sensible default to fall back on.
    log_region: str
    versioning: bool = False


config = Config(
    ShowcaseEnv,
    envs={
        "dev": {
            "region": Region.EuNorth1,
            "log_region": Region.EuNorth1,  # keep everything in one region
            "tags": {"app": "showcase", "env": "dev"},
        },
        "prod": {
            "region": Region.EuNorth1,
            "log_region": Region.UsEast1,  # central log archive
            "tags": {"app": "showcase", "env": "prod"},
            "versioning": True,
        },
    },
)

for env in config.envs():
    with Stack(env.name, config=env):
        # --- Component: one call expands into a bucket + a TLS-only Deny policy.
        #     S3 bucket names are *globally* unique, so the name carries the
        #     environment; without it dev and prod would collide. ---
        site = SecureBucket(
            "web",
            bucket=f"atlantide-showcase-{env.name}-{uuid5('showcase', env.name)[:8]}",  # noqa: F821
            versioning=env.versioning,
        )

        # --- Per-block region override, driven by the environment: this bucket
        #     lives wherever `log_region` says, not necessarily the stack's region
        #     (its `region` field is set by the scope). ---
        with region(env.log_region):
            logs = S3Bucket(
                "logs",
                bucket=f"atlantide-showcase-logs-{env.name}-{uuid5('showcase-logs', env.name)[:8]}",  # noqa: F821
            )

        # --- Output combinators over apply-time refs (arns/domains unknown until
        #     apply): concat two parts, interpolate a template, join a list. ---
        output("site_url", interpolate("https://{}", site.domain_name))
        output("objects_glob", concat(site.arn, "/*"))  # arn:aws:s3:::web-bucket/*
        output("bucket_domains", join(", ", [site.domain_name, logs.regional_domain_name]))
