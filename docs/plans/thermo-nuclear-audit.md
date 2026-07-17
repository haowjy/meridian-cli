# Thermo-nuclear audit — synthesis map

Closes #389.

Full-structure audit of meridian-cli against one lens: **eliminate bug classes by
construction**. Performance regressions count as a bug class. This PR is the map —
verdicts, keep/steal/move calls, and cross-links to every workstream it fed. New
defects are filed as issues and owned by workstream PRs; nothing lands here but
the synthesis.

## 1. Method

- **17 finder lanes** over the qi/knowledge tree: 9 subsystem lanes (ops, harness,
  launch, cli, state, streaming, foundation, extension-surface, periphery),
  3 peer-benchmark lanes (omnigent, vercel-ai HarnessAgent, seven-system corpus
  gap-check), 4 cross-cutting lanes (layering, performance, concurrency,
  invariants), and 1 documentation lane (xc-docs).
- **76 raw code findings** (`findings/merged.json`, indexed [0]–[75]) plus 16 doc
  findings (`findings/xc-docs.md`). Doc findings skipped the panel by design —
  the code citation is the proof — and were **fixed in place** on
  `task/qi-docs-hygiene` (commit `0accba00`), together with code-doc finding [16]
  (state AGENTS.md citing the nonexistent `reap_spawns()` and a deleted
  `temp_worktree_store.py`).
- **Two-source adversarial panel**: the 72 panel-eligible findings were grouped
  into 10 clusters (A–J), each verified independently by (a) a native Fable
  refuter and (b) a sol reviewer spawned on a different harness/model family
  (`meridian spawn -a reviewer --skills thermo-nuclear-review`). Both sides
  opened every cited file:line; several bugs were **reproduced with runtime
  probes** (§4). Where both sources adjusted a claim, the adjusted version is the
  finding; refuted details stay dead.
- **Orchestrator adjudication** of the 5 panel disagreements: [50] dropped,
  [19] dropped, [40] dropped, [29] reclassified dead-code, [31] kept narrowed
  (§6). Merges: [27]→[61], [70]→[60], [34]→H-cluster core; both panels also
  identified [13]/[45] as one defect, presented merged below.
- 3 peer-defend findings ([32], [37], [42]) validated against actual peer
  checkouts in `~/.meridian/ref/` (§3).

## 2. Verdict summary

| Cluster | Findings in | Panel outcome | Routed to |
|---|---|---|---|
| A write-seam & locking | [60] [61] [62] [63] [64] [67] (+[27]→[61], [70]→[60]) | 3 confirmed **with reproduced failures**, 5 adjusted (proposal-level), 0 refuted | `task/concurrency-by-construction` |
| B atomic-write canon | [48] [65] [66] (+[50]) | 3 adjusted-confirmed; [50] refuted → dropped | `task/concurrency-by-construction` |
| C typed state model | [68] [69] [71] [72] [75] [74] [36] | all confirmed/adjusted; probes reproduced nonterminal finalize + cross-harness event misclassification | `task/typed-state-contracts` (cross-links: [75]→#378, [74]/[36]→#375) |
| D work-item model | [73] [15] | both adjusted-confirmed; crash ambiguity reproduced | `task/typed-state-contracts` |
| E layering cycles | [43] [44] [45] (+[13]) [46] [49] [51] [38] [4] | all confirmed/adjusted; 68/47 import-edge counts independently reproduced by both panels | `task/layering-and-decomposition` ([4] perf half → #376) |
| F god files | [0] [1] [8] [9] [21] [24] [47] | all confirmed/adjusted (census exact: 14 files >1k lines, 20.2% of src) | decomposition issue batch; [9] → #375 |
| G config single-parse | [7] [10] [20] [22] [25] [57] | all confirmed/adjusted; probes measured 12/21 TOML parses per read/write prepare (doubled by [10]) | invocation-scoped-config issue batch |
| H read amplification | [3] [6] [14] [17] [18] [52] [53] [54] [55] [56] [58] [59] (+[34]) | all confirmed/adjusted; timings measured (0.39s `list_spawns` @ 3k rows vs 250ms poll; 0.19s session replay @ 19k events) | #376 (index/wait/sessions/Pi-history) + local-memoization issue batch |
| I dup policy & dead code | [5] [11] [12] [23] [26] [28] [30] [2] (+[19], [29]) | 8 confirmed/adjusted; [19] refuted → dropped; [29] reclassified dead-code | dedup/dead-code issue batch; [11] fixes a live `--fork` bug |
| J peer steals | [31] [33] [35] [39] [41] (+[40]) | [31] narrowed (manager gap reproduced); [33]/[41] already tracked (#373/#374); [35]/[39] demoted to design issues; [40] refuted → dropped | #377 ([31]); #375 (evidence for #373/#374); design issues ([35], [39]) |
| xc-docs | F1–F16 + [16] | fixed in place, no panel (per audit charter) | qi-docs-hygiene PR (commit `0accba00`) |

Net: of 76 raw findings, **3 dropped**, **4 merged into siblings**, **1 fixed in
place**, remainder confirmed or adjusted and routed. No finding shipped
unverified.

## 3. Keep / steal / move vs peers

### Keep — validated strengths, defend with evidence

| Strength | Evidence | Peer contrast |
|---|---|---|
| [32] Descendant-aware completion over crash-recoverable state | resident/Pi completion holds until every transitive descendant `state.json` is terminal; reaper classifies outcomes (`orphan_finalization`/`orphan_run`/`timed_out`) rather than kill-and-rebuild | omnigent has no analog: `subagents` is a flat capability boolean; sub-agent dispatch is fire-and-forget; crash recovery is orphan-PID reconciliation only |
| [37] Journaled permission/approval broker | `permission_broker.py` persists every approval as an append-only sequenced journal with pending/resolved/failed/cancelled transitions — crash recovery independent of the orchestrator | Vercel's `pendingToolApprovals` is an in-memory Map; its durability is outsourced to the Workflow DevKit. Do not "simplify" 499 lines toward a shape that only works with an external durable-workflow dependency |
| [42] Transitive drain evidence as one canonical helper | `ReconciledDescendantEvidence` shared by resident and Pi paths — single helper, not per-harness forks | SYNTHESIS.md §2: "Nobody does descendant-aware completion. Nobody." Any Pi/resident drain consolidation must not fork or dilute this helper |

### Steal — adopted (as corrected by the panel)

- **[36] Typed stream-event contract** (Vercel `HarnessV1StreamPart` discriminated
  union + compile-time assignability guard) → adopted into `task/typed-state-contracts`,
  corrected: keep an open `RawHarnessEvent` transport envelope and normalize into a
  closed semantic union — meridian ingests unpinned third-party streams and must
  never hard-fail on unknown upstream events. One move with [74].
- **[34] Parent-indexed descendant lookup** (omnigent WAL/indexed query) → adopted
  as the concrete hot loop that sizes #376's derived index; no database (repo
  constraint), merged into the H-cluster core.
- **[31] Cancellation-safe subprocess ownership** (omnigent `except BaseException`
  + shielded reap, "exactly one owner") → adopted **narrowed** (§6): the real gap
  is the `SpawnManager.start_spawn()` registration window and stdio children
  without scope sidecars, not all five adapters. → #377.
- **[38] Capability-table over harness-name branching** (peer-corpus synthesis
  steal #3) → adopted into `task/layering-and-decomposition`, corrected: neutral adapter-owned
  continuation enrichment or persisted metadata, not Pi-named capability flags;
  evaluate deleting the legacy Claude fallback first.
- **[33]/[41] Absolute ceiling / rearm budget** (omnigent idle+absolute watchdog
  pair) → already tracked as #373/#374 in #375; the panel added corrections that
  belong in that plan: an opt-in absolute ceiling already exists
  (`--timeout`/`MERIDIAN_TIMEOUT` arms a non-renewing attempt timer,
  `streaming_runner.py:739-786`) — the gap is default-off; Pi deadlines are
  anchored per window, and the unboundedness comes from successive waves
  re-anchoring, so model per-window deadlines and total lifetime as separate
  clocks.
- **[39] Diff/change-preview surface** (claude-squad/vibe-kanban/crystal all have
  first-class diff viewers) → adopted as a design issue, corrected: `meridian
  spawn files` already exists as an artifact-derived change surface
  (`cli/spawn.py:1022-1048`); worktrees are not auto-provisioned; the real gap is
  a git-optional diff against a **recorded base**, not naive `git diff` in a
  possibly-shared checkout.

### Move rejected — steals refuted

- **[40] `resume_fidelity` capability field**: premise false — Vercel has no
  static recovery-fidelity capability (its docs explicitly say there is no static
  capabilities object). The field would have zero consumers and conflates
  completed-session resume with in-flight-turn recovery. Dropped.
- **[31] as originally framed** ("all five adapters leak, add `except
  BaseException` everywhere"): meridian's disk-recorded process scopes + reaper
  cleanup + Linux PDEATHSIG already delete the claimed class — and survive
  SIGKILL, which in-process guards cannot. Only the narrowed residue kept.
- **[35] "wiring-only" reattach**: real gap (the reaper actively terminates
  still-alive resident backends, `reaper.py:594-609`), but safe continuation
  needs endpoint/turn/cursor/lease persistence plus ownership fencing — a new
  subsystem boundary. Vercel's `suspendTurn()` is a planned handoff, not crash
  recovery. Demoted to a design issue.

## 4. The three headline bug-class eliminations

Each headline names a bug class held today by convention, the abstraction that
makes it unwritable, and the reproduced failure that proves the class is live.

### 4.1 One mutate-under-lock seam per store (write-seam class)

Every store that composes read-lock/release + mutate + write-lock/release loses
updates; the spawn store additionally has an unlocked "owner tier" that rewrites
whole records from a stale cache. The sol reviewer **reproduced four lost-update
failures** via legal public-API interleavings:

```text
spawn_clobber      {'status': 'running', 'harness_session_id': None}   # locked writer set it; stale owner write erased it
archive_lost_update ['p2']                                             # concurrent archive: p1's entry erased
work_concurrent_result {'description': '', 'task_dir': '/tmp/new-task'} # task-dir writer erased the description update
hook_lost_update   {'hook-b': '...'}                                   # whole-dict rewrite erased hook-a's timestamp
```

Evidence: `spawn/repository.py:260-291` (unlocked `write_state`, revision counter
written and never read — `rg '\.revision'` finds only the incrementer),
`lib/spawn/archive.py:52-56`, `work_store.py:599` (the module's only lock) vs
`:769-936` unlocked RMW, `hooks/interval.py:38-90`.

The abstraction: every mutation is a pure function applied under the store's
lock (`write_state_locked` / `mutate_archived_spawns` / `_mutate_item` /
`run_if_due`), read APIs become pure, whole-record replacement goes private.
The composing API — separate public read and write — disappears, so the lost
update becomes **unwritable**. [70] dissolves here: once the unlocked tier is
gone, "ownership" stops being a write-safety concept. [64] rides the same seam:
persist a cleanup claim under `state.lock` and kill at-least-once (crash-only),
never before the terminal CAS.

### 4.2 Lock identity (split-brain class)

`platform.locking.lock_file` never revalidates that the path still names the
locked inode, while session cleanup unlinks lock files and pruning rmtree's
spawn dirs containing `state.lock`. **Reproduced**:

```text
lock_split_brain true    # holder kept the old inode locked; second process locked the recreated path — both inside the exclusive region
```

Three divergent lock implementations exist; `session_store.py:307-343` privately
implements the correct fstat-vs-stat revalidation loop, proving the codebase
already pays for the fix. Corrected remedy (both panels): revalidation alone is
insufficient and "only unlink while held" is not portable to Windows — give
locks stable identities outside destructible directories (or never unlink lock
files), then consolidate all three implementations into one parameterized
primitive preserving plugin timeout/shared-mode semantics. Deleting a lock file
then degrades to a retry instead of a mutual-exclusion breach, regardless of
caller discipline. The atomic-write half of the cluster ([48]/[65]/[66]) is the
same shape one level down: one crash-durable replacement primitive exported
through `plugin_api`, enforced by a repo-wide AST conformance test (ruff TID251
cannot see method calls on inferred `Path`), with reproduced autosync failures
(shared fixed tmp path → `FileNotFoundError` + wrong content published;
concurrent user edit to AGENTS.md silently destroyed).

### 4.3 Typed status / fact clusters (illegal-state class)

Spawn status vocabulary lives in three places (`domain.py:14` Literal,
`spawn_lifecycle.py:52-66` str frozensets with a TODO admitting it, persisted
plain `str` laundered through four bare `cast()`s); terminal and runner-exit
facts are ~38 independent Optionals that writers couple and the type does not.
**Reproduced**:

```text
finalize_spawn(..., status="finalizing")  # accepted: writes exit_code/finished_at/terminal_origin on a NONTERMINAL row
Claude tool_call   -> turn_active          # cross-harness event misclassification, unscoped activity set
Pi session.idle    -> idle
work-store crash:  archive interrupted    -> reads back as open/archived_at=None — byte-identical to an interrupted reopen
```

Readers already ship invented answers for the permitted-but-never-written
states: the reaper guesses exit codes 0/130/1 (`reaper.py:203-219`, duplicated
at `ops/spawn/query.py:84-99`); `spawn_service.py:1066` defaults exit_code to 1.
The abstraction: one `SpawnStatus` StrEnum owner with ACTIVE/TERMINAL derived
from members (parse vocabulary = SpawnStatus ∪ {"unknown"} — quarantine, never
coerce corrupt values to `failed`); frozen `RunnerExitFacts`/`TerminalFacts`
sub-models with `terminal: TerminalFacts | None` as the discriminant — partial
terminal state becomes **unparseable, including on disk**; finalize APIs accept
only terminal statuses; one shared field-schema base kills the ~6 hand-copied
parallel field lists whose drift is silent persisted data loss ([72]); and a
per-adapter `HarnessSemantics` table dispatched by `harness_id` before
`event_type` deletes the cross-harness collision class ([74]/[36]). The
work-item store is the same class ([73]/[15]): directory location becomes the
sole archived-ness authority, transitions go location-first, and the 117-line
heuristic reconciliation codec is deleted — with `status` kept open-vocabulary
(`"done"` reserved) because custom labels are a product feature the original
StrEnum proposal would have destroyed.

## 5. Cross-link map

Everything this audit fed, by destination:

| Destination | What it received |
|---|---|
| **`task/concurrency-by-construction`** (new PR) | Clusters A+B: single mutate-under-lock seam per store ([60]+[70], [61]+[27], [62], [64], [67], with the [55] read-collapse rider), lock identity ([63]), atomic-write canon through plugin_api + AST conformance test ([48], [65], [66]). Member issues #391–#399. |
| **`task/typed-state-contracts`** (new PR) | Clusters C+D: status/facts/schema typing ([68], [69], [72], [75]), typed mutation outcome ([71]), per-adapter event semantics ([74]+[36]), work-item model ([73]+[15]). Member issues #400–#405. |
| **`task/layering-and-decomposition`** (new PR) | Cluster E: state←ops runtime-root inversion ([45]+[13]), launch↔harness contract extraction ([43], gated by [46]), core hosting an application service ([46]), harness specifics out of launch/ops ([44], [38], [4] layering half), ops→cli→ops Mars loop ([49]), plugin_api facade direction ([51]). Member issues #406–#415. |
| **#375** post-drain-convergence streaming | [9] `execute_with_streaming` decomposition (with the corrected update_spawn-batching caveats); panel evidence for #373/#374 (opt-in ceiling exists, default-off; separate window vs lifetime clocks); [58] flag-only append-syscall note; [74]/[36] cross-reference (semantics table lands in TYPED-STATE). |
| **#376** state-store scaling | The audit's biggest feeder: parent-indexed subtree lookup sized by the reproduced 4 Hz full-scan ([17]/[34]/[52]/[53], with the immediate behavior-preserving judo: collect descendants on raw rows, reconcile only descendants); metadata-only wait reads + staged reconciliation evidence ([3]/[54]); sessions.jsonl scope extension — per-session authoritative `state.json`, not a dual-write snapshot cache ([14], measured 0.19s/read @ 19k events); single-pass Pi-gated history telemetry ([56], perf half of [4]). Issue drafts S1–S4. |
| **#377** spawn startup watchdog | [31] narrowed: cancellation-safe ownership transfer in `SpawnManager.start_spawn()` (reproduced leak) + scope sidecars for stdio children. Issue draft W1. |
| **#378** Claude session-identity | [75]'s identity-typing half (ChatId/HarnessSessionId normalization at construction) cross-referenced into its plan. |
| **#379** CLI correctness | [62] cross-reference (work_store seam lands in CONCURRENCY; #379's work-activation items should build on it, not around it). |
| **#380** spawn lifecycle observability | [66] cross-reference (conformance-test allowlist doubles as the observability write inventory). |
| **qi-docs-hygiene PR** (`task/qi-docs-hygiene`, 0accba00) | All 16 xc-docs findings + [16]: 24 dead KB links, ghost REST/four-adapter architecture, deleted-store documentation, migration diaries/benchmarks/tombstones, wrong counts, broken subpackage index. Fixed in place. |
| **Unbundled design issues** | [35] in-flight turn continuation after runner death (new subsystem boundary, needs a product decision); [39] git-optional diff against a recorded base. Issue drafts D1–D2. |
| **Standalone issue batches** | G-cluster invocation-scoped config (G1–G2); F-cluster god-file decomposition (F1); H-cluster local memoizations (M1); I-cluster dedup/dead-code (X1–X3). |

Sequencing note (from the panels): [46] gates [43] (core must stop hosting the
application service before the shared contract moves into it); [68] gates [72]
(stored-model field types must converge before SpawnRecord can compose
StoredSpawnState); the CONCURRENCY spawn-seam issue gates the [55] read
collapse (unsafe under the current two-tier writer model).

## 6. Explicitly out — dropped findings

| Finding | Why dropped |
|---|---|
| [50] canonical `read_json_object` helper | The four "copies" implement different contracts: one parses SQLite column values (no file, no OSError), one needs a missing-vs-unreadable taxonomy that `dict | None` collapses; only 2 shallow duplicates remain — a cross-layer API would be a leaky abstraction. |
| [19] drain-coordinator forwarding mixin | The `DrainCoordinator` protocol + typed `DrainPlan.coordinator` field already enforce shape statically; a mixin of ten trivial forwards is inheritance hiding explicit composition. |
| [40] `resume_fidelity` capability field | Premise false: Vercel has no static recovery-fidelity metadata; the field would have zero consumers and conflates two recovery concepts. Revisit only if [35] becomes a product requirement. |
| [29] (reclassified, not dropped) | The Mermaid JS validation tier is unreachable: `mermaid-validator.bundle.js` was never committed, built, or shipped, so `detect_tier()` always selects Python. Not a performance bug — **dead code**: delete the tier or ship the bundle (draft X3). |
| [31]/[35] original framings | Kept only as narrowed/demoted (§3); the original claims ("all five adapters leak", "reattach is wiring") were refuted with evidence. |

---

## Work item

thermo-nuclear-audit

Closes #389
