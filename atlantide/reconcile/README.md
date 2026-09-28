# atlantide.reconcile

Desired IR against current state: what changes, in what order, and the execution
of those changes against real providers.

| Module | Purpose |
| --- | --- |
| `changes.py` | The diff's output model: `Change`, `ChangeSet`, `restrict` (`--target`), `FORCED_FIELD`, and `type_mutability`, which builds the per-type field mutability table the diff classifies by. |
| `diff.py` | The whole-graph pass: classifies each node id into CREATE / UPDATE / REPLACE / DELETE / NOOP (Merkle skip, `--replace`, stale dependents, re-attribution to moving upstreams). Comparison is symbolic (properties keep their `$ref` markers), matching the Merkle hash. |
| `classify.py` | The per-node rules the diff applies (`classify_node`), and `reclassify`, which re-runs them over resolved values for a conditional REPLACE at apply. |
| `upstream.py` | Whether an upstream value moved, as the diff reads it: the `Consumed` verdicts and which of them to trust (`settled`, `unrecorded`), and `touches` (a field referencing an upstream that changes this run). Pure; the verdicts are computed in `applied.py`. |
| `ordering.py` | Replace ordering over a ChangeSet: `create_first` (from `graph.cbd.effective_cbd`), `resolve_cbd` (the planner's fallback to destroy-first when a create-first replacement would collide with the old identity, or its refusal when a create-before-destroy dependent forbids that), and `behind_destroy_first`. |
| `applied.py` | What each `$ref` field resolved to when its node was last applied: the per-row `ref_digests` record written with every row, the plan-side `consumed` verdicts against the upstreams' stored outputs, and `matches`, which the apply's re-check uses. |
| `guards.py` | Enforces the `prevent_destroy` guard over a ChangeSet (`check_prevent_destroy`), deferring a protected conditional REPLACE to apply (`deferred_to_apply`). |
| `executor/` | Runs the ChangeSet: applies forward over the desired graph, deletes in reverse over the prior-state graph, confirms conditional REPLACEs (`confirm.py`), runs both REPLACE strategies (`replace.py`) and the deletes (`deletes.py`) over one shared run context (`context.py`), persists state per node (`records.py`), compensates on failure under `on_failure="rollback"` (`saga.py`), and commits stack outputs (`outputs.py`). |
| `writer.py` | `StateWriter`: state writes off the event loop, as concurrent as the backend allows, with lock calls kept exclusive of them; `SerializedBackend` routes the lock scaffold's calls through it. |
| `refresh.py` | Reads live provider state and reports drift; `write=True` folds the result back into state. |
| `resolve.py` | Resolves `Ref`, `SecretRef`, `StackOutputRef`, and `$transform` handles to values, and rebuilds a live `Resource` from a stored node. |
| `adopt.py` | Import (`adopt`, `AdoptOptions`): reads an already-existing resource through its provider, checks it against config, and writes the state row an apply would have written — so the next plan is a NOOP rather than a CREATE. Calls no mutating provider method. |
| `aliases.py` | Rename-without-replace: rekeys prior state from an old node id to a new one and re-hashes affected dependents. |
| `env.py` | What a run executes against: `ApplyEnv` (providers, backend, secrets, lease, events), `Desired` (one compiled config), `OnFailure`, `DEFAULT_NODE_TIMEOUT`, and the per-node helpers `provider_for` and `node_failure`. |
| `progress.py` | Per-node progress: the `Phase` enum, the `ProgressCallback`/`RefreshProgress` shapes and their no-op defaults, and `progress_sink` (callback → event stream). |
| `state_ir.py` | Persisted state read back as a graph: `ir_from_state`, `state_digraph` (delete ordering, alias re-hashing). |
| `report.py` | `ApplyReport`: what one run did, per action. |

The Merkle `input_hash` is a function of config alone, so three channels carry a
state-side change into the diff: `NO_INPUT_HASH`, written by `refresh --write`
when live inputs drift; `_stale_dependents`, which pulls a node out of NOOP
when an upstream node is being recreated under it; and the `ref_digests` record
(below), for an upstream output that moved since a dependent was last applied.

## What a `$ref` field consumed

Properties, IR and hash are symbolic, so a stored row cannot say which upstream
value its resource was given. An apply that moves an upstream's outputs (a
renamed bucket's `regional_domain_name`) and stops before a dependent (a
CloudFront distribution's immutable `origin_domain`) — interrupted, failed, or
narrowed by `--target` — leaves the dependent's row equal to the config marker
for marker. Before the record, the next plan diffed it as an UPDATE with no
changed fields (or, when no hash moved, NOOP) and the apply pushed an immutable
value through `update()`.

Every row the executor or `import` writes therefore records, per `$ref`-bearing
property, a digest of the value it resolved to (`StateNode.ref_digests`, see
`applied.py` for the two digest schemes; a value derived from a `sensitive` field
is digested with the install salt, never stored). At plan, `applied.consumed`
resolves the config's markers against the upstreams' *stored* outputs and
reports each field whose value differs from the record. The diff trusts that
verdict only where every upstream the field references keeps its outputs this
run: there, the move is **known** — an `immutable()` field makes an
unconditional REPLACE (so `prevent_destroy` refuses it at plan), a mutable one an
UPDATE naming the field, even for a node the Merkle skip would pass over. The
field is listed in `Change.upstream_moved` (and `upstream_moved` in `plan
--json`). Behind an upstream that changes again this run the value is unknown,
and the usual conditional logic applies.

A row written before the record (empty `ref_digests`) cannot vouch for its
values. Its ref fields are consulted only when nothing else explains the node's
hash mismatch (no field changed, not a poisoned row): they are then treated as
possibly moved — a conditional REPLACE for an immutable one, confirmed at apply
against the run-start outputs as before the record, which cannot see a move an
earlier run made. Every row written from then on carries a record.

## `prevent_destroy` changes

`prevent_destroy` is not part of the Merkle hash, so a node whose only difference
from state is the flag diffs as NOOP. It is a **state-only** NOOP
(`Change.state_only`): `ChangeSet.pending` includes it (`actionable` does not,
since no provider is called), the plan fingerprint covers it, and the executor
rewrites the row with the new flag through the same fenced writer as every other
row, with no provider call and no hash change. `ApplyReport.state_only` lists
those rows. Which flag the guard reads is the planner's business
(`engine.planner.protected_ids`): the config's for a declared node, the stored
one for a node the config drops.

## Conditional REPLACE

A REPLACE is *conditional* (`Change.conditional`, shown as "known after apply")
when every immutable field it changes carries a `$ref` whose value is only known
at apply. The executor reaches such a node after its upstreams have applied and
confirms it (`executor.confirm.Confirmation.confirmed`) before any write or provider call:

- the desired properties resolve against the upstreams' new outputs and are
  compared with the row's `ref_digests`, the values the node was last applied
  with (the outputs the run started from are no substitute: an earlier run may
  have moved them). A field the row records nothing for (a literal, a pre-record
  row) is compared with the stored row resolved against the run-start outputs.
  `classify.reclassify` classifies the concrete values with the diff's own rules;
- an immutable value that moved confirms the REPLACE, unchanged (create-before-
  destroy or destroy-before-create as planned); a protected node is refused
  here with `PreventDestroyError`;
- otherwise the node runs as an UPDATE of the fields that moved, or, if none did,
  as a NOOP that still rewrites its row with this config's input hash,
  properties, flags and ref digests (outputs, provider version and secret
  digests are kept),
  so the next plan Merkle-skips it. No `creating` row is ever written for a
  replace that collapses.

A REPLACE with an immutable ref to an upstream replaced destroy-before-create is
never conditional (`ordering.behind_destroy_first`, re-run by the planner after it
downgrades a colliding create-before-destroy): it is replaced unconditionally
and its delete half runs before the upstream's delete (the executor's phase 0),
so only a create-before-destroy upstream lets a dependent collapse this way.

The report lists the node under the action actually taken and in
`ApplyReport.downgraded`; its `node_finish` event carries that action with
`detail.planned = "replace"`.

The guard defers a protected conditional REPLACE to apply only while every
upstream it references through a changed field keeps its identity this run;
behind a created or replaced upstream the plan refuses it as before, and a
replace known from the record (above) is not conditional, so it is refused at
plan too. An out-of-band change to an upstream's output that `refresh --write`
folds in moves the value its dependents consume, so the next plan re-applies
them.
