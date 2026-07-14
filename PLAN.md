# Atlantide — Typed Python IaC Engine

## Context

Build a new Infrastructure-as-Code tool (`atlantide`) as a fully-typed Python 3.11+ package — a competitor to Terraform and Pulumi. Beat both by combining Pulumi's real-programming-language ergonomics with Terraform's declarative rigor, while adding three things neither does well:

1. **Determinism by construction via Atlas-lang + compiled IR** — config is written in **Atlas-lang**: Python *syntax* (every config file is valid Python, so IDEs/mypy/formatters work) executed by **our own subset interpreter**, never CPython. Clock, randomness, env reads, network, file I/O **do not exist in the language** — nothing to block, no sandbox to escape. The interpreter lowers config to an **Atlas IR** artifact — the reproducibility contract — which is content-hashed. Plan identity = `hash(IR)`. Nothing downstream re-runs user code. Terraform allows `uuid()`/`timestamp()`; Pulumi allows arbitrary imperative code — both leak non-determinism into plans.
2. **State as a graph** — reconciliation is a graph diff, not a linear state walk. This unlocks fine-grained parallel create/update/delete and content-hash (Merkle) skipping of unchanged subtrees.
3. **Deployable artifacts** — `atlantide build` emits a portable, content-hashed `.atlas` (IR + provider version pins + policy set). `atlantide deploy prod.atlas` applies it anywhere without the source — build-once/promote-everywhere, Docker-style.
4. **Capability-aware providers** — providers declare `Capabilities` (batch create, native transactions, import, events, rollback); the planner/scheduler exploit them (batch independent creates; use native transactions for rollback, fall back to the compensation saga otherwise).
5. **First-class stacks, policies, lifecycles** built into the engine, not bolted on.

**Compile pipeline**: `Atlas-lang (Python-syntax subset) → subset validation (ast) → own interpreter (fuel-bounded) → Atlas IR (hash) → graph build → planner → executor`. The IR boundary is where reproducibility, caching, and language-independence (future Rust engine) all live.

**v1 scope**: prove the architecture end-to-end with the core engine + a **multi-provider registry**, shipping a small **AWS subset** provider (real cloud CRUD) plus a toy `local` provider for tests/CI without creds. The **state layer is modular** — the graph is a logical model behind a `StateBackend` interface so the storage engine is swappable (embedded SQLite default; in-memory and remote backends later).

## Locked design decisions

| Area | Choice |
|------|--------|
| Providers | Own typed Python SDK (async CRUD), no Terraform-protocol reuse. **Multi-provider registry**; every provider is **semver-versioned** (stamped per IR node, pinned in artifact, compat-checked on deploy). Ship an **AWS subset** provider first (+ toy `local` provider for tests) |
| Determinism | **Atlas-lang**: Python-syntax subset run by **our own fuel-bounded interpreter** (never CPython) — non-determinism has no syntax to reach; deterministic set/dict iteration. Lowered to **Atlas IR**; plan identity = `hash(IR)`; downstream never re-runs user code |
| IR & artifacts | **Atlas IR** = canonical serialized form (language-independent, cacheable, content-hashed). `atlantide build` → portable `.atlas` (IR + version pins + policy set); `deploy` consumes it without source |
| Provider capabilities | Providers declare `Capabilities` (batch/transactions/import/events/rollback); planner + scheduler exploit them (batch creates, native-transaction rollback else saga) |
| State | **Modular/agnostic** — graph is a logical model; `StateBackend` ABC swappable. Default = embedded SQLite graph store; in-memory + remote (S3/Postgres) behind the same interface |
| Scope | Core engine MVP + AWS subset provider (+ local toy provider) |
| Modularity | **Everything pluggable via registries**: `ProviderRegistry` (resources), `StateBackend`, `SecretsProvider`, `PolicyProvider` — same swap-in pattern across the board |
| Differentiators (v1) | **Transactional apply + auto-rollback** (saga); **fine-grained subgraph locking**; **modular sealed secrets** (pluggable `SecretsProvider`); **modular + per-resource policies** (`@policy` decorator, pluggable `PolicyProvider`) |

## Why atlantide beats Terraform & Pulumi

| Capability | Terraform | Pulumi | Atlantide |
|-----------|-----------|--------|-----------|
| Language | HCL (non-Turing, limited) | real langs, but imperative/non-deterministic | **Atlas-lang**: typed Python-syntax subset, own deterministic interpreter (dynamic config, determinism **by construction**) |
| Plan reproducibility | `timestamp()`/`uuid()` leak non-determinism | arbitrary code | non-determinism **doesn't exist in the language**; canonical IR, plan id = `hash(IR)`, provably identical |
| Reconciliation | linear state walk | linear | **state-as-graph + Merkle skip** (unchanged subtrees never touch provider) |
| Parallelism | coarse walk (default 10) | coarse | **async DAG scheduler**, per-node concurrency |
| Failure handling | leaves half-applied state | half-applied | **transactional saga: auto-rollback to last-good** |
| State locking | whole-state lock | whole-state | **per-subgraph lease locks** (disjoint applies run concurrently) |
| Secrets in state | plaintext | plaintext (unless configured) | **sealed refs via pluggable `SecretsProvider`** (never plaintext) |
| Immutability rules | per-provider hardcoded | per-provider | **declared per-field** (`immutable()`/`mutable()`/`computed()`) |
| Providers | Go plugins (gRPC) | multi-lang plugins | typed Python SDK, **multi-provider registry**, mixed-provider graphs, **capability-aware** (batch/transactions) |
| Plan as artifact | `.tfplan` (opaque, version-locked) | no portable artifact | **content-hashed `.atlas`** — portable IR, build-once/deploy-anywhere, no source needed |
| Engine portability | Go monolith | language-tied | **language-independent IR** — Python front-end today, Rust engine possible on the same IR |

## Architecture

### Package layout
```
atlantide/
  pyproject.toml                # pydantic v2, typer, rich, stdlib sqlite3; mypy strict, ruff
  atlantide/
    core/
      resource.py     # Resource base (pydantic), typed fields, Meta (provider, lifecycle)
      types.py        # Output[T]/Ref lazy handles, Input[T], Sensitive, secret()
      fields.py       # mutable()/immutable()/computed()/force_new_if() field helpers -> pydantic Field metadata
      provider.py     # Provider ABC: name + semver version; async create/read/update/delete/diff + Capabilities; Context
      capabilities.py # Capabilities: supports_batch_create/transactions/import/events/rollback (+ batch hooks)
      registry.py     # ProviderRegistry: name->Provider; version resolution + semver compat check; per-provider config/creds
      context.py      # ApplyContext: creds, logger, dry_run
      errors.py       # NonDeterministicError, PolicyViolation, DriftError, CycleError
    lang/
      validate.py     # ast.parse + subset check: reject disallowed nodes with precise errors
      interp.py       # tree-walking whitelist evaluator: lexical scopes, fuel counter, deterministic semantics
      builtins.py     # curated deterministic stdlib (len/range/sorted/zip/math subset, str/list/dict methods) + atlantide.input()/secret()
      stack.py        # Stack: namespace, isolated state, cross-stack reference resolution
    ir/
      model.py        # Atlas IR: canonical typed nodes+edges+inputs -> deterministic JSON
      lower.py        # lower evaluated config (ResourceRegistry) -> Atlas IR
      hash.py         # content-hash of canonical IR (plan identity)
      artifact.py     # .atlas bundle: IR + provider version pins + policy set; build/load/verify (hash + pins)
    graph/
      model.py        # DiGraph: nodes(id)->Resource, edges(dependency|reference)
      build.py        # build graph from Atlas IR (edges from IR refs + depends_on); cycle check
      schedule.py     # asyncio parallel scheduler (Kahn ready-set + semaphore)
    state/
      backend.py      # StateBackend ABC (get/put/delete node, edges, lock, serial) — storage-agnostic
      model.py        # StateGraph value type: nodes+edges+Merkle hashes, backend-independent
      sqlite_backend.py   # default: SQLite impl (WAL) of StateBackend
      memory_backend.py   # in-memory impl for tests/CI
      remote_backend.py   # stub interface for S3/Postgres (later)
      lock.py         # fine-grained subgraph locking (lease-based, per-node ids)
    secrets/
      backend.py      # SecretsProvider ABC: seal(plaintext)->SealedRef, unseal(ref)->plaintext
      ref.py          # SealedRef value type: {backend, key_id, ciphertext} — the only form stored/serialized
      registry.py     # SecretsRegistry: name->configured SecretsProvider (like ProviderRegistry)
      keyfile.py      # local AES-GCM keyfile impl (default, creds-free)
      env.py          # env-var / process impl (dev)
      kms.py          # AWS KMS impl (v1 first cloud backend)
      # roadmap impls behind same ABC: vault.py, sops.py, age.py, gcp_kms.py, secretsmanager.py
    reconcile/
      diff.py         # desired graph vs state graph -> ChangeSet (per-node Action)
      planner.py      # order ChangeSet into a change-DAG; replace detection
      executor.py     # run change-DAG in parallel; persist per node; transactional saga rollback
    policy/
      base.py         # Policy / PolicyProvider ABC: evaluate(ctx, changeset, node)->PolicyResult; levels
      registry.py     # PolicyRegistry: name->configured PolicyProvider (like Provider/Secrets registries)
      decorator.py    # @policy(name, level=...) — attach policies per-resource (class or instance); + global/stack registration
      builtin.py      # native-Python PolicyProvider (required-tags, deny-destroy-in-prod, count cap)
      # roadmap impls behind same ABC: rego.py (OPA/Rego), external.py (webhook engine)
    lifecycle/
      rules.py        # Lifecycle: prevent_destroy, create_before_destroy, ignore_changes, replace_on_change, retain
    providers/
      local/          # toy: File resource (path/content CRUD on disk); Null resource — creds-free tests/CI
      random/         # roadmap: Uuid/Password/Id/Timestamp — value generated once at apply, pinned in state
      aws/            # AWS subset provider (boto3/aiobotocore): S3 Bucket, IAM Role, SQS Queue
                      #   creds via standard AWS chain; each resource = typed Resource + async CRUD
    cli/
      main.py         # typer app: build | plan | apply | deploy | destroy | graph | state | refresh | verify
  tests/
```

### 1. Resource & Provider SDK (`core/`)
- `Resource` is a pydantic v2 model → free typed validation, JSON serialization, and a canonical field hash. Subclasses declare typed inputs; a nested `Meta` binds the provider and `Lifecycle`.
- **Per-property mutability** is declared on each field via `Field(json_schema_extra={...})`, wrapped by a typed helper:
  - `mutable()` — change → **UPDATE in place** (default).
  - `immutable()` — change → **REPLACE** (delete + recreate; force-new).
  - `computed()` — provider-set output, not diffed as input (e.g. ARN).
  - optional `force_new_if(...)` for conditional replacement — **declarative conditions only** (serializable field/value predicates), not arbitrary callables, so deploy-from-artifact can evaluate them.
  ```python
  class Bucket(Resource):
      bucket_name: str = immutable()          # rename => replace
      region:      str = immutable()          # move region => replace
      versioning:  bool = mutable(default=False)   # toggle in place
      tags: dict[str, str] = mutable(default_factory=dict)
      arn: str = computed()                   # output only
  ```
  The diff engine reads this field metadata directly — no separate list to keep in sync. `Lifecycle.replace_on_change`/`ignore_changes` remain as per-*instance* overrides layered on top of the class-level declarations.
- `Output[T]` / `Ref` are lazy handles. Referencing `bucket.arn` in another resource returns a `Ref`; graph build reads these to derive edges. Values resolve at apply time.
- `Provider(ABC)` with **async** `create/read/update/delete` (+ optional `diff`) so independent I/O overlaps. `read` powers drift detection and import. For **transactional rollback**, each mutating op returns/records a compensating action (create→delete new, update→re-apply prior inputs) the executor can invoke to undo it.
- **Multi-provider**: a `ProviderRegistry` maps provider name → configured `Provider` instance (each with its own creds/region/endpoint config). A resource's `Meta.provider` binds it to a registry entry, so one graph can span providers (e.g. AWS + local) and the scheduler drives each node's provider transparently.
- **Versioning (required)**: every provider declares a `name` + **semver `version`**. Lowering stamps each IR node with its `provider@version`; `build` **pins** those versions into the `.atlas` artifact; `deploy`/`apply` resolve the registered provider and **fail on a semver-incompatible version** (major-version mismatch → hard error). State persists each node's applied provider version, so a provider upgrade is detected and can trigger a schema/state migration hook. This makes plans reproducible against a known provider build, not just known config.
- **Capabilities**: each provider declares a `Capabilities` object; the planner/scheduler adapt. `supports_batch_create` → coalesce independent same-type creates into one `batch_create` call. `supports_transactions`/`supports_rollback` → use the provider's native transaction/undo instead of the compensation saga. `supports_import` → enable bulk import. `supports_events` → stream progress. Unset capabilities fall back to the safe generic path.

```python
class Provider(ABC):
    name: str                                       # registry key, e.g. "aws"
    version: str                                    # semver, e.g. "1.4.2" — pinned in the artifact
    capabilities: Capabilities                      # declared per provider
    async def create(self, ctx: Context, res: Resource) -> dict: ...
    async def read(self,   ctx: Context, res: Resource) -> dict | None: ...
    async def update(self, ctx: Context, prior: dict, res: Resource) -> dict: ...
    async def delete(self, ctx: Context, res: Resource) -> None: ...
    # optional, gated by capabilities:
    async def batch_create(self, ctx: Context, res: list[Resource]) -> list[dict]: ...

@dataclass(frozen=True)
class Capabilities:
    supports_batch_create: bool = False
    supports_transactions: bool = False
    supports_import:       bool = False
    supports_events:       bool = False
    supports_rollback:     bool = False   # native undo; else engine uses compensation saga
```

### 2. Atlas-lang: deterministic Python subset (`lang/`, `ir/`)
Config is **Atlas-lang** — Python *syntax*, our own execution. Every config file parses with stdlib `ast` and is valid Python (IDE, mypy, formatters work unchanged), but it is executed by **our tree-walking interpreter, never CPython**. Determinism is **by construction**: no sandbox, no audit hooks, no enforcement to escape — the non-deterministic surface simply has no syntax to reach.
1. **Subset validation** (`lang/validate.py`): `ast.parse`, then reject disallowed nodes with precise errors — imports outside the allowlist, `eval`/`exec`, dunder access, dynamic `getattr`, `while` (unbounded), `class`, `yield`, `async`, `with`, `open`, `global`/`nonlocal`.
2. **Interpretation** (`lang/interp.py`): whitelist evaluator over the AST — module statements, `def`, `for` over finite iterables, `if`/`else`, comprehensions, f-strings, literals and operators, lexical scoping. A **fuel counter** bounds total evaluation steps → no runaway config. Dynamic ergonomics (loops generating N resources, helper fns, computed values) fully preserved.
3. **Deterministic builtins** (`lang/builtins.py`): curated stdlib (`len/range/sorted/enumerate/zip/min/max/sum`, str/list/dict methods, `math` subset) plus **pure derived functions** — `uuid5(namespace, name)`, `sha256`, `b64encode/decode`, CIDR math — deterministic by construction. Clock/random/env/net/file APIs **do not exist**. Set/dict iteration order is **defined** (insertion/sorted), killing the `PYTHONHASHSEED` ordering leak CPython execution would have. Sanctioned inputs: `atlantide.input(...)` records its value as an explicit IR input (visible in plan); `atlantide.secret("KEY")` records only `{env_key, salted_digest(plaintext)}` — change-detectable, **never the value**.
   - **Randomness is a resource, never a function** (`providers/random/`, roadmap): `random.Uuid`, `random.Password`, `random.Id`, `Timestamp` are resources whose value is generated **once at apply**, persisted in state, and stable thereafter (first plan: known-after-apply; then Merkle-NOOP). Same UX as Terraform's `uuid()`/`timestamp()` without the plan-noise bug — regeneration is an explicit REPLACE, visible in plan.
4. **Lower to Atlas IR** (`ir/lower.py`): the evaluated `ResourceRegistry` is lowered to canonical **Atlas IR** — deterministic JSON of resource nodes, inputs, and edges. `ir/hash.py` content-hashes it (**plan identity**). Everything downstream (graph, planner, executor, deploy) consumes the IR, never the config.
- **Spec'd node-by-node**: the evaluator has a written semantics for each allowed AST node → a future Rust engine reimplements the same interpreter against the same subset, same IR out.

### 2b. Atlas IR & deployable artifacts (`ir/`)
- **IR model** (`model.py`): canonical, language-independent form — `{resource, id, provider, provider_version, properties, dependencies, sealed_refs}` per node + edge list. Serializes to **deterministic JSON** (sorted keys, normalized values) so the same config always yields byte-identical IR. This is the layer a future Rust engine would consume unchanged.
- **Artifact** (`artifact.py`): `atlantide build` bundles IR + **provider version pins** (from each provider's declared semver) + the **policy set (names + params + levels — not code)** into a `.atlas` file carrying its `hash(IR)`. `atlantide deploy prod.atlas` verifies the hash, **semver-checks each pinned provider against the registered build** (major mismatch → hard error), then runs graph→plan→apply directly from the IR — **no user source, no re-execution of user code**. Deploy requires the **provider packages installed**: resource classes (with their field-mutability metadata) are rehydrated from the IR via the registry — that's how diff classifies UPDATE vs REPLACE without source. Build once in CI, promote the same artifact dev→staging→prod (Docker-image-style).
- **Caching**: `hash(IR)` is the *config* identity; the plan cache key is **`(hash(IR), state serial)`** — a plan is `diff(IR, state)`, so identical IR alone must not short-circuit re-planning. A hash mismatch on load flags a corrupted/altered artifact.

### 3. Graph model + capability-aware parallel scheduler (`graph/`)
- Built from the **Atlas IR**: node id = `{stack}:{type}:{logical_name}`; edges from IR `dependencies` (derived from `Ref` usage) + explicit `depends_on`. Cycle detection at build → `CycleError`.
- `schedule.py`: asyncio scheduler. Kahn's algorithm maintains a ready-set (in-degree 0); launch all ready nodes concurrently bounded by a semaphore (`--parallelism`, default = cpu*4 for I/O). On each completion, decrement dependents' in-degree and enqueue newly-ready. Reverse the DAG for destroy. Uses `asyncio.TaskGroup`.
- **Capability-aware**: when a set of independent ready nodes share a provider that declares `supports_batch_create`, the scheduler coalesces them into one `batch_create` instead of N calls.

### 4. State — modular graph store (`state/`)
- **Agnostic interface**: `StateBackend` ABC exposes graph ops — `load_graph()`, `put_node`, `delete_node`, `get_edges`, `acquire_lock`/`release_lock`, `serial`. The engine only ever talks to this interface; `StateGraph` (in `model.py`) is the storage-independent value type (nodes + edges + Merkle hashes). Swapping backends never touches reconcile/executor code.
- **Default backend** = SQLite (`sqlite_backend.py`, WAL, ACID, no server). Tables:
  - `nodes(id PK, stack, type, name, input_hash, output_json, status, provider, provider_version, lifecycle_json, updated_at)`
  - `edges(from_id, to_id, kind)`
  - `meta(key, value)` — schema version, serial, lock owner/expiry.
- **Other backends** behind the same ABC: `memory_backend.py` (tests/CI), `remote_backend.py` (S3/Postgres, stubbed in v1).
- **Merkle `input_hash`, two-phase** (dependency outputs are unknown until apply): **plan-time** hash = `hash(canonical_inputs_with_symbolic_refs + sorted(dependency plan hashes))` — computable without provider I/O; **apply-time** hash recomputed once upstream outputs resolve, then persisted. A node is NOOP-skippable only when its hash is computable **and no upstream node changed**. Incremental persist: executor writes each node as its CRUD succeeds → partial applies are crash-safe and resumable, regardless of backend.
- **Fine-grained locking** (`lock.py`): locks are scoped to the **subgraph being changed** (the changeset's node ids + their dependency closure), not the whole state. Two applies touching disjoint subgraphs run concurrently; overlap → the second waits or fails fast. Locks carry owner + expiry (lease) to survive crashed clients. Backend-agnostic: SQLite backend implements it with a `locks` table keyed by node id; remote backends map to their native lease primitive.
- **Sealed secrets (modular)**: fields marked `Sensitive`/`secret()` are stored as **`SealedRef`s** (never plaintext; redacted in plan/logs/graph export) and unsealed only in-memory at apply time. Sealing/unsealing goes through a **pluggable `SecretsProvider` interface** (`secrets/`) with multiple implementations (local keyfile default, env, AWS KMS; Vault/SOPS/age/GCP-KMS/Secrets-Manager on the roadmap) — swappable exactly like the state and provider layers. **Ciphertext never enters the canonical IR hash** (AES-GCM nonces make ciphertext non-deterministic — it would break byte-identical IR); change-detection uses the salted plaintext digest instead. The rest of the state blob is stored as-is (no whole-blob encryption).

### 5. Reconciler (`reconcile/`) — the core optimization
- `diff.py`: match desired vs state nodes by id:
  - desired-only → **CREATE**
  - both, `input_hash` equal → **NOOP** (skipped entirely — no provider `read`, this is the Merkle win; sound only when no upstream node changed — see two-phase hashing)
  - both, hash differs → compute the **changed-field set**; if any changed field is `immutable()` (or matches `replace_on_change`/`force_new_if`) → **REPLACE**, else **UPDATE in place**
  - a field consuming a changing upstream output is **known-after-apply**: if that field is `immutable()`, plan shows **REPLACE (conditional)** and the action is finalized during apply once the value resolves
  - state-only → **DELETE**
  - `computed()` and `ignore_changes` fields are excluded from the changed-field set (and from `input_hash`), so provider-set outputs never trigger spurious replaces.
- `planner.py`: assemble a change-DAG. Creates/updates in topological order, deletes in reverse; `create_before_destroy` splits a REPLACE into (create new → rewire deps → destroy old). **Identifier-collision guard**: when the replaced resource's unique identifier is itself `immutable()` (e.g. S3 bucket name), CBD is impossible — planner falls back to destroy-before-create with an explicit warning (or requires a name change).
- `executor.py`: runs the change-DAG through the parallel scheduler; persists state per node. **Transactional apply**: if the node's provider declares `supports_transactions`/`supports_rollback`, use its **native transaction/undo**; otherwise fall back to the **compensation saga** — each node records a compensating action (undo) as it succeeds, and on failure the executor halts then runs compensations in reverse dependency order to roll back to last-good (`--on-failure=rollback|halt|continue`). Rollback is persisted incrementally so a crash mid-rollback resumes.

### 6. Policies (`policy/`) — modular + per-resource
- **Modular engine**: a `PolicyProvider` ABC (`evaluate(ctx, changeset, node) -> PolicyResult`) with multiple swappable implementations selected via a `PolicyRegistry` — same pattern as `ProviderRegistry`/`SecretsRegistry`. v1 ships the native-Python `builtin` provider; OPA/Rego and external-webhook providers slot in behind the same ABC (roadmap).
- **Per-resource attachment via decorator**: `@policy(name, level=...)` stacks on a Resource class (or instance) to bind named policies to just that resource. Policies can also be registered globally or per-stack.
  ```python
  @policy("require-tags", level="mandatory")
  @policy("no-public-acl", level="mandatory")
  class Bucket(Resource):
      ...
  ```
- **Pure policies** evaluate at **plan** time — builtin policies are engine code (or Atlas-lang), so they stay deterministic. **External engines** (OPA/webhook) do network I/O — the sandbox blocks sockets — so they run as a separate **gate stage** outside the determinism guarantee; their verdicts are not part of reproducible planning. Levels: `advisory` (warn) / `mandatory` (block apply). The engine gathers each node's decorator-bound policies + global/stack policies, resolves each name through the registry, and runs them.
- **Artifacts carry policy names + params + levels — never code.** Python policy callables are not serializable into a language-independent artifact; implementations must be installed at deploy (same rule as providers).

### 7. Lifecycles (`lifecycle/rules.py`)
- `Lifecycle(prevent_destroy, create_before_destroy, ignore_changes=[...], replace_on_change=[...], retain)`. Per-*instance* overrides layered over the class-level field mutability (`immutable()`/`mutable()`). Consumed by diff/planner. `ignore_changes` fields excluded from `input_hash`; `prevent_destroy` turns a planned DELETE (or a REPLACE's destroy half) into a hard error.

### 8. Stacks (`lang/stack.py`)
- `Stack` namespaces resources and owns an isolated state graph (its own SQLite file / table prefix). Cross-stack references resolve through a typed `StackReference` that reads another stack's committed outputs.

### 9. CLI (`cli/main.py`, typer + rich)
- `build` (compile config → content-hashed `.atlas`), `plan` (diff + policy eval from IR, rich tree output), `apply`, `deploy <artifact>` (verify hash + pins → apply from IR, no source), `verify <artifact>` (hash + pins check), `destroy`, `graph` (export dot/mermaid), `state list|show|rm|import`, `refresh` (provider `read` → drift report).

## Tech choices
- **pydantic v2** — typed resources, validation, canonical hashing.
- **asyncio** (`TaskGroup`) — scheduler/executor parallelism.
- **sqlite3** (stdlib, WAL) — default state backend (swappable).
- **aiobotocore / boto3** — AWS subset provider (async where possible).
- **typer + rich** — typed CLI + readable plan output.
- **stdlib `ast`** — Atlas-lang parsing/validation (grammar comes free from CPython); **own tree-walking interpreter** — execution; **stdlib `hashlib`** — IR content-hashing.
- **Python 3.11+**; **mypy --strict** + **ruff** across the package.

## Build milestones (implementation order)
1. `pyproject.toml`, package skeleton, tooling (mypy/ruff/pytest).
2. `core/` — Resource, types (`Output`/`Ref`), Provider ABC + `Capabilities`, `ProviderRegistry`, Context.
3. `lang/` — `validate.py` (subset check) + `interp.py` (fuel-bounded evaluator) + `builtins.py` → `ir/` (model, lower, hash, artifact). Determinism boundary.
4. `graph/` — model, build **from IR** (edge derivation, cycle check), capability-aware parallel scheduler.
5. `state/` — `StateBackend` ABC + `StateGraph` model + SQLite + in-memory backends + Merkle input_hash + **fine-grained subgraph locking**.
5b. `secrets/` — `SecretsProvider` ABC + `SealedRef` + `SecretsRegistry` + keyfile/env/KMS impls.
6. `reconcile/` — diff (Merkle skip), planner (replace/CBD), executor (incremental persist + **transactional saga rollback**).
7. `providers/local/` — File + Null (creds-free); prove multi-provider plumbing.
8. `providers/aws/` — S3 Bucket, IAM Role, SQS Queue (typed resources + async CRUD via aiobotocore/boto3); declare `name`, semver `version`, `Capabilities`.
9. `cli/main.py` — build/plan/apply/deploy/verify/destroy/graph/state/refresh.
10. Wire `policy/` (`PolicyProvider` ABC + `PolicyRegistry` + `@policy` decorator + builtin) and `lifecycle/` into diff/planner.
11. Tests.

## Verification (end-to-end)

**Local provider (creds-free, primary CI path)** drives the full loop:
1. Config declaring several `File` resources with dependencies (one file's content references another's output).
2. `atlantide plan` → N creates in correct topo order; policy pass; deterministic (run twice, identical plan).
3. `atlantide apply` → files created on disk in parallel where independent; state populated with nodes + edges + input_hashes.
4. Modify one file's input, re-`plan`/`apply` → only changed node + dependents UPDATE; unchanged nodes NOOP (**Merkle skip proven** — assert provider CRUD not called).
5. `atlantide destroy` → reverse topo order; `prevent_destroy` blocks with clear error.
6. Drift: hand-edit a file, `atlantide refresh` → drift reported.

**AWS provider (real cloud, gated on creds)**:
7. Config with an S3 Bucket + IAM Role + SQS Queue where the Role policy references the Bucket ARN (cross-resource edge). `plan` shows correct order; `apply` creates real resources in parallel where independent; state records ARNs/outputs.
8. Re-`apply` unchanged → all NOOP (Merkle skip, zero AWS calls). Change a tag → single UPDATE. `destroy` removes in reverse order.
9. Also verify a **mixed-provider graph** (AWS + local in one config) applies through the registry.

**IR / artifact checks**:
- **Deterministic IR**: compile the same config twice → byte-identical IR and identical `hash(IR)`; a config with a loop generating N resources lowers to N IR nodes.
- **Atlas-lang**: a config importing `socket`/`subprocess`, calling `eval`, or using `while`/`class`/dunder access is rejected at validation with a precise error; `time`/`random`/`os.environ` are simply undefined names; set/dict iteration order is stable across runs (no `PYTHONHASHSEED` leak); an infinite-loop-shaped config hits the fuel limit and errors.
- **Build/deploy**: `build` → content-hashed `.atlas`; `deploy prod.atlas` applies with the source directory absent; a corrupted/altered artifact fails the `hash` check on load.
- **Provider versioning**: IR nodes carry `provider@version`; the artifact pins it; deploying against a **major-incompatible** registered provider hard-errors, a compatible (minor/patch) one proceeds; state records the applied provider version per node.

**Differentiator checks**:
- **Provider capabilities**: with a mock provider declaring `supports_batch_create`, N independent creates issue one `batch_create` call (assert call count); with `supports_transactions`, rollback uses the native path not the saga.
- **Transactional rollback**: inject a failure at node K mid-apply → assert nodes 1..K-1 are compensated (undone) in reverse order and state returns to last-good; kill the process mid-rollback → resume completes it.
- **Fine-grained locking**: two concurrent applies on disjoint subgraphs both succeed; two on overlapping subgraphs → second waits/fails fast; a stale (expired-lease) lock is reclaimable.
- **Modular sealed secrets**: a `secret()` field is stored only as a `SealedRef` — never appears in plaintext in the state file, plan output, logs, or `graph` export; unseals correctly in-memory for apply. Same secret config runs against ≥2 `SecretsProvider` impls (keyfile + a mock KMS) with identical results, proving the backend is swappable.
- **Modular + per-resource policies**: a `@policy`-decorated resource is evaluated only by its bound policies; a `mandatory` failure blocks apply, `advisory` warns; policies not attached to a resource don't run for it. Register the same policy through a second `PolicyProvider` impl (builtin + a mock external engine) → identical verdicts, proving the engine is swappable.

**Backend-agnostic check**: run the same reconcile suite against both `sqlite_backend` and `memory_backend` — identical results prove state modularity.

**Unit tests**: scheduler ordering + concurrency (independent nodes parallel, deps respected); cycle → `CycleError`; interpreter: disallowed nodes rejected, undefined non-deterministic names, fuel exhaustion, sanctioned `input()`/`secret()` tracked; diff → correct CREATE/UPDATE/REPLACE/DELETE/NOOP; **field mutability** — changing an `immutable()` field yields REPLACE, a `mutable()` field yields in-place UPDATE, a `computed()` field yields NOOP; Merkle skip avoids provider calls; `create_before_destroy` + `prevent_destroy`; incremental persist survives simulated mid-apply crash (resume completes remaining nodes); AWS provider CRUD tested against **moto** mock so cloud tests run in CI.
