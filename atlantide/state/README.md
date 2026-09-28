# atlantide.state

The graph state store: one record per resource, plus committed stack outputs and
the per-node lock leases.

The engine talks only to `StateBackend`; concrete backends are interchangeable
behind it and selected declaratively through `make_state_backend`. The remote
backends stay out of the package namespace so their dependencies load only when
configured.

| Module | Purpose |
| --- | --- |
| `model.py` | `StateNode` / `StateGraph` value types and `NodeStatus`. |
| `leases.py` | `Lease`, `LockPolicy` and its defaults, the skew margin, and the run-side `LeaseGuard`. |
| `fencing.py` | Who may write or lock a node (`fence_violation`, `scope_conflict`) and the wording of every refusal, shared by all backends. |
| `backend.py` | The storage-agnostic `StateBackend` interface and the shared outputs merge. |
| `codec.py` | Serialization shared by every persistent backend: canonical row columns for table stores; the snapshot document (S3, and `state backup` files) and journal entries. |
| `factory.py` | `StateConfig` → a configured backend. |
| `memory.py` | In-process, volatile. Same semantics as sqlite. |
| `sql/sqlite.py` | Default: embedded SQLite in WAL mode, single file, ACID. |
| `sql/postgres.py` | Shared remote state, one row per node. Requires the `postgres` extra. |
| `sql/schema.py` | The one `nodes` column spec both dialects' DDL, added-column migration and node row statements are generated from; `meta`/`locks` per dialect. |
| `sql/postgres_sql.py`, `sql/postgres_preflight.py` | The postgres backend's statements and psycopg Protocols; its `state check` rows. |
| `sql/common.py`, `sql/dsn.py` | What the two share (lock rows as leases, the contended-lock signal); DSN parsing and redaction without the driver. |
| `s3/` | Shared remote state: an S3 snapshot plus a per-node journal, DynamoDB heads (commit pointers + fences), one lease item per run and a lock row per node; compaction and fsck. `backend.py` is a façade over collaborators sharing one `S3Context` (`context.py`, and its one mutex): `snapshots`, `reads`, `view`, `writes`, `outputs`, `lock_table` (acquire/renew/release/admin; over `lease_items` for the lease items and lock rows, `fences` for the fence counter, and `acquire` for the chunked takeover), `maintenance`; `journal.py` is the pure half (key layout, heads, folding a consistent cut); `limits.py` the tunables; `preflight.py` the `state check` rows and the conditional-write probe. |

Writes are incremental: a node's row lands the moment its provider call
succeeds, so a crash mid-apply leaves a consistent state that a re-run resumes
from. `replace_many` exists for moves (an alias rekey), which must never be
observed holding neither id.

## S3 backend: the state journal

Neither the bucket nor the tables are auto-created: they are the trust root for
shared state and must exist, with versioning on the bucket, a lifecycle rule on
the journal, and point-in-time recovery on the heads table, before atlantide is
pointed at them.

### Layout

- **Snapshot** at `key`: every node and output as of the last fold, with
  per-node watermarks `wm` (per-stack `owm` for outputs), a raise-only `fences`
  floor, `max_fence`, a fold generation `gen`, and the journal `epoch`. It is
  created (empty, minting the epoch) by the first write or lock, never by a read.
- **Journal entries** at `key.d/<epoch>/log/<node>/<seq>-<fence>-<nonce>.json`
  (one node write) and `key.d/<epoch>/out/<stack>/…` (a stack's whole output
  map).
- **Heads** in DynamoDB (the lock table by default, or `[state].journal_table`):
  one item per node (`\x00h\x00{ns}\x00{epoch}#{node}`) and per stack
  (`\x00o\x00…`) holding `{fence, seq, ref, op, state_ns}`. Heads have no
  `owner`, `namespace` or `expires_at`, so `locks()`, `state unlock` and the TTL
  never see them.
- **Leases** in the lock table: one *lease item* per acquisition
  (`\x00l\x00{ns}\x00{lease_id}`: `{lease_id, owner, fence, lease_ns,
  lease_expires_at, expires_at, revoked?}`) and one *lock row* per node
  (`{ns}#{node}`: `{namespace, node, owner, fence, lease_id}`). See
  [Locking](#locking).
- A node's value is the entry at `head.ref` when `head.seq > wm[node]`, else the
  snapshot's.

### Writes and fencing

- **A write** stores its entry (`If-None-Match: *`), then commits it with one
  `UpdateItem` on the head, conditional on the head's seq (the one the write was
  based on) and, under a lease, on `head.fence` being exactly the lease's fence.
  That commit is the durability point the executor awaits. A condition failure
  whose head already points at this entry is a retry after a lost response, i.e.
  success. Otherwise: head fence higher → superseded (the holder is named from
  the lock rows); lower → never recorded; same fence but the seq moved →
  rebase onto the head (another unlocked writer, or a stale view).
- **Concurrency.** `write_concurrency = 16` (per instance via
  `[state].write_concurrency`): writes to *different* nodes may be in flight at
  once; the cached view is guarded by a lock. Lock operations are called with no
  write in flight.
- **Outputs** are one entry per changed stack plus a transaction over the stack
  heads (seq-conditional), fenced by `ConditionCheck`s on the bound lease's
  nodes in those stacks, the same rule as the other backends. Acquire does not
  fence stack heads: otherwise two runs over disjoint nodes of one stack (or a
  `state rm`) would supersede each other's outputs.

### Locking

Liveness is per lease, not per node: a lock row only names the lease it belongs
to, and holds its node exactly while that lease item exists, is not `revoked`,
and has not lapsed `lock_skew_margin` past `lease_expires_at`.

- **Acquire** mints a fence (counter in the lock table), `PutItem`s the lease
  item, then takes the scope in 50-node chunks, up to 16 chunks in flight at
  once. Each chunk is one transaction of [lock-row `Put`, head
  `Update SET fence` if not lower] per node. The row `Put` is conditional on the
  row being absent or pointing at a lease this acquire may take from; the first
  attempt allows only the new lease (plus leases other chunks already found
  takeable), so a free scope costs no reads. A row refused by its condition is
  read, its lease item read and judged: gone, revoked, or this owner's own →
  takeable; lapsed past the margin → *revoked* (one conditional `UpdateItem`,
  deduplicated across chunks) and takeable; otherwise it is a live holder and
  the acquire fails naming it. The chunk is then retried with those leases
  allowed. Revoking outside the chunk's transaction is safe because revocation
  is permanent (ids are never reused and `revoked` is never cleared). Keeping
  the old lease out of the transactions means parallel chunks never contend on
  one item (a `TransactionConflict`). A chunk refused at a head
  (a newer fence) or by a live holder stops chunks not yet started; the acquire
  then deletes its lease item (which alone frees every row pointing at it) and
  the rows it wrote.
- **Renew** of the bound lease is one `UpdateItem` of its lease item, whatever
  the scope size: `SET lease_expires_at, expires_at` if `owner` and `fence`
  match and it is not `revoked`. Every takeover revokes the lease it
  takes from *before* taking any row, so a hold that lapsed, was taken and was
  released again between two heartbeats still fails the renewal. A lease that
  lapsed with nothing taken renews: nobody was granted its nodes. A lease item
  that is gone (released, or reaped) fails the renewal too. An unbound renewal,
  or one over a wider scope, is an acquire.
- **Writes** are fenced at the heads only. A takeover raises every head it takes
  in the same transaction as the row, so fence equality alone proves no newer
  lease holds the node; a commit need not also check the lease item. A
  revoked lease may keep committing to nodes nobody took: none was granted to
  anyone else.
- **Release** deletes the owner's lease items first (freeing its rows at once;
  a crash after that leaves only free rows), then its rows in parallel, each
  only while it is still this owner's.
- **TTL.** The table's TTL attribute is `expires_at`. Lock rows have none, so
  DynamoDB never reaps the rows of a live lease however long a run lasts. A
  lease item's `expires_at` is `lease_expires_at + lock_skew_margin` (pushed out
  by every renewal), so the reaper frees a crashed run's rows no sooner than a
  taker could; its rows then point at a missing lease, i.e. are free, and are
  overwritten by the next acquire or removed by `state unlock`.
- **Administration.** `locks()` scans this namespace's rows and reads their
  lease items: a row's expiry is its lease's (`0` when the lease is gone or
  revoked). `state unlock` deletes rows only; the lease item stays, since it may
  still hold nodes that were not broken. The broken run keeps renewing, and if
  another run then takes a broken node, the raised fence refuses the broken
  run's writes to it (it learns at its next write, not at its next renewal).
- **Cost** (on-demand, 1 KB items; a transactional write is 2 WRU). With `N`
  nodes: acquire ≈ `4N` WRU (`N` row `Put`s + `N` head updates, transactional)
  in `⌈N/50⌉` transactions, 16 at a time; at 50k nodes that is 1000
  transactions, ≈ 2–3 s (30–50 s sequentially), ≈ $0.12. A renewal is 1 WRU
  (≈ $6·10⁻⁷) and one round trip at any `N`. Release is `N + 1` single-item
  deletes (`N` WRU), 32 at a time. A takeover adds per chunk one refused
  transaction and two `BatchGetItem`s, plus one revoke per old lease.

**Non-atomic operations:**

- *Chunk-level fencing during acquire.* A scope over 50 nodes is several
  transactions (run in parallel); each chunk is fenced atomically, not the
  whole scope. Any commit made to a node before its chunk was fenced is kept and
  seen by the new holder.
  An acquire that fails part-way releases its lock rows but leaves the fences it
  already raised; that blocks only the superseded lease, never a live one.
- *Bulk writes* (`put_many`/`replace_many`: alias rekeys, `state restore`,
  `state migrate`) fold the journal and write the snapshot under `If-Match`,
  atomically for readers. The fence check is a *pre-check*: the touched heads are
  re-read just before the PUT, and a moved seq rebases, a moved fence refuses.
  A takeover landing in the one round trip between that re-read and the PUT is
  not caught. A bulk write changing a single node uses an ordinary commit.
- *Outputs over 100 items.* Up to 100 stack heads plus fence checks commit in
  one transaction. Beyond that the fences are pre-checked (same window as bulk
  writes) and the stacks commit in chunks of 100, each chunk atomic but not the
  whole write.

### Reads

GET the snapshot (`If-None-Match` after the first read: usually a 304), LIST the
journal, `BatchGetItem` the heads of items with entries above their watermark,
GET the committed entries, then HEAD the snapshot: if it changed (a fold or bulk
write landed) or an entry 404s, the read restarts (5 attempts). A head naming an
entry the LIST missed was committed after it, so the journal is re-LISTed (3
times) to pick up whatever it depends on. A compacted state costs GET + LIST +
HEAD and no DynamoDB call.

**Reader permissions.** Any read (`plan`, `state list/show`, `output`) needs
`s3:GetObject` on `key` and `key.d/*`, `s3:ListBucket` on the `key.d/` prefix,
and `dynamodb:BatchGetItem` (plus `GetItem`) on the heads table. Writers also
need `s3:PutObject`/`s3:DeleteObject` and `dynamodb:PutItem`, `UpdateItem`,
`TransactWriteItems`, `DeleteItem` and `Scan` (lock administration, fsck).

### Compaction and GC

Lock-free: read a cut, fold every effective entry into the snapshot (watermarks
move to the folded heads' seqs, fences only rise, `max_fence` absorbs the fence
counter, serial is unchanged), write it under `If-Match` (a lost swap abandons
the fold), then delete every listed entry at or below its new watermark. Commits made
after the cut stay above the watermark. An entry one past its head (a writer
between PUT and commit) is never deleted. Triggers: `checkpoint()` after every
locked run, a background fold every 1000 commits, and `atlantide state compact`.
`plan` never compacts.

On a versioned bucket every deleted entry leaves a noncurrent version: add a
lifecycle rule on `key.d/` with `NoncurrentVersionExpiration` (e.g. 7 days) and
`ExpiredObjectDeleteMarker`. `state check` warns when none covers the prefix.

### Serial

`serial = snapshot.serial + Σ (head.seq − wm)` over the effective heads: every
commit adds exactly 1, a fold leaves it unchanged, a bulk write adds 1.

### Losing the heads table

The heads are the commit pointers: if the table holding them is deleted or
recreated, every commit since the last fold disappears from the view (the
entries remain). Keep point-in-time recovery on (`state check` warns otherwise).
`atlantide state fsck` reports lost heads and heads whose entry is missing;
`--rebuild-heads` re-points each lost head at its highest-seq entry (the higher
fence on a tie). That entry may be a write that never committed, so every
rebuilt node is listed for review. An entry written under the fence its head still
holds, with no commit, is a run that died before committing: it is reported as
pending, never rebuilt. A fence counter found missing is re-seeded 1,000,000
above the snapshot's `max_fence`, in the same atomic update as the bump, so it
cannot re-mint a fence a paused run still holds.

### Format

The snapshot is format 3 and is the only format read. Formats 1 and 2 are
refused with migration instructions: with the release that wrote the state,
`atlantide state migrate --to-local old.db`; with this one,
`atlantide state migrate --from old.db --force`. `state backup` files
are format-3 snapshots with empty journal bookkeeping.

### Clock skew and namespaces

- A lapsed hold is taken over only once it is `[state].lock_skew_margin` seconds
  (default 30) past expiry by the taker's clock, so a host with a fast clock
  cannot take a live lease.
- Lock rows are keyed `s3://bucket/key#node_id` with a `namespace` attribute,
  lease items carry it in their key and as `lease_ns`, and heads as `state_ns`,
  so projects sharing one table never contend, and `state unlock --all` lists
  and breaks only this state's rows.

## Postgres backend: locking and fencing

- **Fences are rows.** Each acquisition mints a fence from a counter in `meta`
  and upserts the `locks` rows in one transaction. Every write (`put`,
  `delete`, bulk writes, and `set_outputs`) reads the lock rows it depends on
  `FOR UPDATE` inside the same transaction as the write, so a takeover cannot
  land between the check and the write.
- **Outputs** (`{stack}:{name}`) are fenced on the bound lease's nodes in each
  stack whose outputs are written or removed. A stack in which the lease holds
  no node (outputs declared over no resource) is merged unfenced; the `outputs`
  row lock still keeps two concurrent merges from losing either.
- **Server time.** Expiries are written and judged against the database's
  `clock_timestamp()`, so client clocks never decide who holds a lease. A
  lapsed hold is still taken over only `[state].lock_skew_margin` seconds
  (default 30) after expiry: the same rule as S3, here acting as grace for a
  holder whose renewal is late. Leases returned to the caller and listed by
  `locks()` are translated to the local clock (same remaining time), because
  `LeaseGuard` and `state unlock` compare against `time.time()`. The
  constructor's `clock` argument replaces the server clock and exists for
  deterministic tests only.
