## Why

Three independently-filed issues describe one architectural gap: every spawn-store read path scales with total store size (full `state.json` scans, whole-file `history.jsonl` reads), and there is no derived index or canonical event stream to read instead. A fourth issue shows the store boundary accepts raw `str` paths, pushing normalization to call sites.

## Goal

One coherent state-store design pass: a validated, rebuildable derived index over spawn state, bounded history reads, a canonical per-spawn event log, and typed path handling at the store boundary — consistent with the files-as-authority + crash-only constraints.

## Summary

Planning draft. Scope, per issue:

- **Closes #359** — history.jsonl: stop unconditional whole-file `read_bytes()` on writer open; add segmentation/checkpointing so growth is bounded. (Triage: high priority — cost paid on every writer open.)
- **Closes #360** — filtered spawn listing: stop O(N) `state.json` reads; filter via the derived index.
- **Closes #274** — define the core child-spawn index contract (`children.json` or equivalent); `pi_subspawn_tracker.py` is a natural first consumer.
- **Closes #129** — canonical per-spawn `events.jsonl`; stop deriving spawn state from raw log heuristics.
- **Closes #211** — normalize path fields at the state-store boundary (`start_spawn` takes `Path`, store owns `.as_posix()`), replacing per-call-site normalization; this also hardens the POSIX path-in-detail contract established in #368.

## Resulting Behavior

Spawn listing and history access cost is bounded by result size, not store size; state derivation has one authority; path form is decided in exactly one place.

## Changes

These share one design shape (derived, rebuildable artifacts over authoritative files) and must be sequenced as one design pass — #274's contract first, #129/#360/#359 building on it, #211 independent and first-mergeable.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/state-store-scaling.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/state-store-scaling.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B

# Extension for PR #376 plan doc (`docs/plans/state-store-scaling.md`)

Append as a new section after **Changes**; add the three new member issues to **Summary** and the Closes list.

---

## Audit findings (thermo-nuclear audit #389, cluster H)

The audit's read-amplification cluster (13 findings; all survived adversarial + independent
verification, with corrections) measured the hot paths this PR's derived index exists for,
and adds consumer-side work the index alone does not fix.

### Measured hot paths — why #360/#274 are the priority

- `list_spawns()` full scan (per-row `state.json` Pydantic parse, `spawn_store.py:893-912`):
  **0.22–0.40 s at 1.5–3k spawn rows** on live stores. Pi descendant polling re-runs it every
  **250 ms** (`pi_drain.py:62`), resident every 5 s (`resident_drain.py:378`) — at measured
  store sizes one scan already exceeds the Pi poll interval. The scan runs synchronously
  inside async `assess()`, blocking the event loop (`descendant_evidence.py:66-83`).
- Ordering makes it worse: `_read_blockers` peek-reconciles **every active row project-wide**
  (full `report.md` read + full `launch-boundary.jsonl` parse per row, `reaper.py:120-150`)
  *before* `collect_descendants` filters to the subtree (`descendant_evidence.py:66-74`).
- `spawn wait` calls `read_spawn_row` per pending id per 250 ms tick (`ops/spawn/api.py:1729-1742`);
  each read includes `starting-prompt.md` (`include_prompt=True` default,
  `spawn_store.py:915-925`) though only `row.status` gates the loop; nested mode adds up to
  6 activity stats per active row (`ops/spawn/query.py:35-42,72-81`, early-exit on first fresh).
- Sessions repeat the pre-v2 read shape: every single-session accessor replays all of
  `sessions.jsonl` (`session_store.py:231-288`); **measured 0.08–0.19 s per read at
  3.4–7 MB / 10–19k events** — disproving `state/.context/CONTEXT.md`'s documented exemption.
  Write rate is slower than the legacy `spawns.jsonl` (a few events per session, not per
  harness event), so real but below the spawn paths in priority.
- Finalization: extractors re-read and re-parse the same JSONL artifact per accessor —
  counting-store probe measured **5–10 `ArtifactStore.get()` calls per finalization**
  across the five harnesses (`harness/common.py:289-319`, `launch/extract.py:141-187`).
- Pi detail: two bespoke full `history.jsonl` scans for the same event type, run for every
  harness with no Pi gate (`ops/spawn/query.py:400-511,542-608`).

### Panel corrections that bound the remedies (do not re-litigate)

- The derived index accelerates listings and subtree lookups, **not point reads** — wait
  polling additionally needs a metadata-only read path or a batch status query.
- Prefer **staged evidence loading** (cheap PID/activity evidence first; read `report.md` /
  launch-boundary only on branches that consult them — cf. `reaper.py:296-299` never reading
  either) over lazy dataclass properties that hide filesystem I/O.
- The one-read rewrite of `write_state_locked` is **unsafe**: owner writes deliberately
  bypass the external per-spawn lock (`core/lifecycle.py:733-747`), so `write_state`'s
  internal re-read protects terminal monotonicity and cancel-intent preservation
  (`spawn/repository.py:274-283`) and must stay. Deletable: caller pre-reads
  (`spawn_store.py:621,668,740,794` — but `finalize_spawn`/`mark_finalizing_with_snapshot`
  use theirs as missing-file handling, so deletion must add explicit `FileNotFoundError`
  handling) and the prompt reads (preserving stored `prompt_length`).
- `inbound.jsonl` has **no production reader** and duplicates `control_actions.jsonl`
  (`harness/control_action.py:100-151`); the O(n²) per-inject recount
  (`spawn_manager.py:536-554,790-794`) is best fixed by deleting the log, not caching a count.
- The `_apply_spawn_filters` per-record `model_dump()` (`spawn_store.py:865-890`) is
  deletable, but the getattr replacement must keep unknown-filter-keys-ignored semantics
  via a missing-sentinel, not a `None` default.
- Per-event append syscall overhead (`state/atomic.py:77-91`) is **flag-only**: fsync is the
  durability contract and likely dominates; no handle-lifecycle change without a profile.
- The session fix must **not** be a dual-written snapshot cache over the global log (a crash
  between JSONL append and snapshot write leaves silent staleness). Make per-session state
  authoritative — `sessions/<chat_id>/state.json`, the proven spawn-v2 idiom — and reserve
  replay/scan for bulk callers.

### New member issues

- **Polling consumers** (findings [3][53][54][56]): descendants-first reconciliation,
  metadata-only wait reads, staged reaper evidence, single-pass Pi-gated history scan.
  [53]'s reorder is behavior-preserving and index-independent — first-mergeable.
- **Finalization & local memoizations** (findings [6][18][55][59]): finalization-scoped
  artifact context; delete `inbound.jsonl`; narrowed locked-mutation read reduction;
  sentinel-getattr filters.
- **Session store** (finding [14]): per-session authoritative state file; deletes the
  unenforced CONTEXT.md exemption.

The unifying shape is unchanged from this PR's Goal — one derived/parent-indexed read path
at the store seam ([17]/[34]/[52] are #360's root cause, now with measurements) — plus the
consumer-side issues above, which are what makes an unscoped `list_spawns()` on a poll loop
unwritable rather than merely faster.
