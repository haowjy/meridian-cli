# Pi result delivery

Every coordination file in the table lives under `pi-bash/<parent>/` except
`observed-launches.json`, which lives in the parent's spawn directory,
`spawns/<parent>/`. Results are consumed per launch, not per spawn: a continue keeps the
spawn ID, so a ledger entry names a **launch key** `"<spawn id>:<n>"` (a Pi bash task keeps
its bare ID). Missing
coordination files mean empty; present malformed or wrong-parent authority means
unknown, never successful empty. Task logs are different: missing, unreadable or
non-regular logs are unavailable, not empty output.
Atomic writes use the shared JSON helper. One parent has one execution writer,
one launch-scoped notification owner, and one Python public-event observer.

| File | Writer | Shape and purpose |
|---|---|---|
| `bash-records.json` | managed Bash | v1 task records, written at lifecycle transitions only (`log_bytes` is the size at the last one); a terminal status is present only once published; tracked terminal background rows stay owed until wait/output consumption or admission. `execution_error` on a row and file-level `runtime_error` preserve unresolved execution/storage evidence. |
| `launch-receipts.json` | spawn watcher | `{v:1,spawn_id,messages:{delivery_id:work_ids[]}}` with launch keys for spawns; exact native custom-message admission, never `sendMessage()` return. |
| `delivery-observations.json` | Python | `{v:1,spawn_id,observed_message_ids:[]}`; exact public admission event observed after marking the parent active. |
| `observed-launches.json` (in `spawns/<parent>/`) | CLI `spawn wait`, `spawn ack`, observed terminal report-body show/status/report-show, and observed `spawn cancel` results reported as `cancelled` | v1 parent, durable `observed_launches` (launch keys), diagnostic `waiting_launches`, `wait_reservations` keyed by caller with `owner_pid`, `owner_birth_epoch`, `expires_at_epoch`, `launches`, and `wait_pins`: `{target spawn id: {n, expires_at}}`, the launch an explicit `spawn wait` is waiting on, which survives `--yield-after-secs` re-runs (a pin dies when its launch is consumed, when `expires_at` passes, or when a continue of the target clears it). Only live matching process leases suppress temporarily. Writers update it under the parent's published-spawn lock. Shared beyond Pi: Python's model of the same schema is `lib/state/spawn_observations.py`, and Claude's `spawn collect` reads this file for every primary spawn of a chat, so a schema change updates the extension and that module in the same change. |
| `cleared-launches.json` | spawn watcher UI | v1 parent, finite `updated_at_ms`, `cleared_launches`; explicit dismissal. Reads union every owned parent's file; only the current spawn's file is written. |
| `delivery-fault.json` | spawn watcher | `{v:1,spawn_id,operation:"scan"|"admission"|null,error:string|null}`; bounded delivery diagnostics independent of execution records. |

**Files from before launches** (`delivery-receipts.json`, `cleared-spawns.json` under `pi-bash/<parent>/`, `observed-spawns.json` in `spawns/<parent>/`) key spawns by bare ID. They are read and never written: a bare spawn entry means launch 1 (an older build cannot read a v4 row, so it only consumed launch 1), a bare bash ID is read as written. The Pi mirror `pi-bash/<parent>/observed-spawns.json` is retired: not read, not written. An older build mixed in re-delivers results the new build consumed, once per run of that older build; it never loses one.

The watcher treats canonical direct `parent_id` rows as this session's children and
delivers only those launched with `launch_mode: "background"`: a launcher that waited for
a foreground child already has its result, so only the launching Bash record can still
owe one. That rule is core's `state.spawn_ownership.is_owed_child`, which the extension
mirrors as `isBackgroundLaunch`; change the two together. An `originating_bash_id` on a
background delivered row transfers the launcher's obligation
to its child. Which parents count as direct is core's rule, not a second copy: a live
primary owns the direct children of every primary generation of its chat, so a
`--continue` resume delivers what the crashed generation launched, while a nested or
spawned session owns only its own children. Meridian computes that parent set at launch
and passes it in `_MERIDIAN_PI_OWED_PARENT_IDS`; the variable is absent outside a
Meridian launch, where the watcher scopes itself to the current spawn. Logs, timers,
origin sidecars, and remembered scans cannot supply child authority. A disposable
candidate set, keyed by launch (`<spawn id>:<n>`), scopes frequent idle reads, but each row is
reparsed and must still have an exact parent in that set. The state scan adds the spawn's
current launch, so a child whose `launch.n` moved is a new candidate even though its earlier
launch was consumed, and adds every `owed_launches` entry whose parent is in the set: such a
launch has moved past `state.json`, so its row is `launches/<n>/launch.json` and its report
is in that launch's directory. A terminal candidate leaves that set once it is consumed,
and the all-generation consumption sources are read only while a terminal candidate is
still undecided under the current spawn's own sources, so an idle tick stays proportional
to what is still owed rather than to the chat's history. Directories without state are
not idle-poll candidates; periodic discovery covers their delayed publication.
Before sending a bare Bash result, discovery runs after observing its terminal
state: cached absence cannot prove there is no canonical child owning that
launcher's obligation. That specific scan's committed revision must remain current
through synchronous claim/send; superseded Bash members stay owed without
withholding independently revalidated spawn members. Successful tool boundaries do a
fresh full discovery; the idle path requests the same discovery once a main-loop
`agent_end` has opened an idle stretch. The
nonrecursive root watcher accelerates idle discovery, and a 30-second full
reconciliation recovers missed events or delayed state rows.
If an ordinary 500 ms idle poll already owns the single flight at reconciliation,
the next tick discovers the row, normally within about 30.5 seconds plus collector
and filesystem time. This cadence is not a hard delivery deadline.
Every full-discovery attempt marks discovery dirty. Failure, a concurrent newer
attempt, or a root event during the scan leaves full discovery owed; only the newest
still-current successful scan publishes candidates and clears that obligation.
Candidate-only success cannot clear a fault from failed full discovery.
While Pi is active, these timer and watcher requests perform no disk I/O. An idle
scan already in progress may finish its current filesystem await, but stops before
reading another row and cannot commit its partial hints. The next tool boundary
supplies fresh discovery.

Publication has two serialized paths: a successful tool-bearing `turn_end` awaits
a fresh scan and queues native steer before the next inference, while genuinely
late work retains the native-idle follow-up fallback. Both recheck consumption
after bounded direct `report.md` reads. Each collection can queue at most one
automatic message. It considers no more than 32 members for that message, reads
the largest deterministic prefix whose worst-case mandatory frames and separators
fit, then reads only those reports sequentially. Before starting each report it
stops if the request's abort/activity generation is no longer current. Each report
read is capped at 64 KiB; the complete joined notification is independently capped
at 64 KiB of UTF-8 content, including status/error/command labels, separators, and
truncation/transcript guidance. After reserving every selected member's mandatory
frame, the collector divides the remaining optional-detail bytes among the members
still to render. Short details leave their unused share available to later members;
a sole member can use nearly the whole packet. UTF-8 truncation stops at a
code-point boundary.
Every included member retains a compact identity and terminal-outcome prefix plus
a concrete full-report/transcript or exact-ID Bash output lookup. The presence of
a result error belongs in that mandatory prefix; its diagnostic text is optional.
Only worker, diagnostic, report-excerpt, and command details may be truncated. A
large optional body on the first canonical member cannot starve the backlog. A member
whose mandatory representation does not fit after earlier members remains owed and
becomes first on a later opportunity. Missing or empty reports retain outcome and
transcript guidance; present unreadable evidence fails closed. Explicit wait
commands still use the CLI.

Bash tool results include outcome evidence independently of log contents. An
available empty log can accompany success, nonzero exit or an execution fault;
none may be inferred from the text alone. Terminal wait/output consumes only when
available output and its outcome are returned, with consumption persisted first.
Unavailable logs, incomplete exit evidence and execution faults remain owed;
an empty fault diagnostic is still a fault. Wait cancellation is checked inside
the acknowledgement slot. Cancellation before staging leaves work owed; an already
staged acknowledgement may land and returns its result rather than pretending to
roll back. Artifact reads, `/ps` previews
and `/pslogs` are inspection, never consumption. Automatic Bash notices retain
execution-fault details under the same bounded-detail rules as spawn errors.

A tracked background terminal result stays owed during active tool waits, so a
terminal wait can persist consumption before the boundary runs. The idle path
checks idleness before its final snapshot and again before queueing, in the same
generation invalidated by `agent_start`, rebind, stop, and exact admission of a
triggering delivery. Queueing the triggering message closes idle scheduling for
that native opportunity; exact admission keeps it closed and invalidates stale
work even if receipt persistence faults. This prevents a second backlog message
from queueing before the inference starts. A later tool boundary or genuinely late
idle transition provides the next opportunity; there is no drain-until-empty loop.

The collector is finite: after reading only its bounded publication slice, it
rereads those selected canonical child IDs independently of the hint cache, takes
one final evidence snapshot, and subtracts consumed, leased, claimed, or changed
initial members. It never substitutes new arrivals. Unrelated root churn cannot
withhold an otherwise stable selected child. Excluded, new, and unstable work
remain owed. Delivery before the next inference is not guaranteed when an attempt
cannot obtain stable, readable evidence. One message identity reserves the exact
published IDs through native admission. Its custom message contains
`details.delivery_id` and `details.work_ids`; the native `message_start` hook
persists precisely that membership before the public RPC event.

Steer joins Pi's native queue and is ordered by Pi with any other queued input; it
is not a priority channel over user steering. Native admission means the message
entered the context, not that the model acted on it. Queueing closes automatic
publication for that native opportunity. If Pi never admits the message (for
example, the queue is cleared), its IDs remain reserved and unrelated automatic
publication can also pause until a later real native lifecycle callback, reload,
or restart reopens collection; timer and watcher wakeups alone do not. This limit
predates boundary steer and is unconfirmed as a reproduced defect.

The boundary path carries the native operation abort signal and publishes only for
completed outcomes with actual tool results. Error, aborted and non-tool turns do
not open a publication window or force continuation. Every shutdown stops watcher
timers before Pi invalidates the old context. Hot reload retains the launch-scoped
owner and queued claims, then rebinds and restarts it at fresh session startup. A cold
restart has no old native queue: unreceipted terminal results retry, receipts and
explicit consumption stay consumed. Neither a reload nor a restart delivers on its
own: a session start (`startup`, `resume`, `reload`, `new`, `fork`) starts the
watchers but opens no idle stretch, because a resumed lead's prompt cache is cold
and an automatic turn before the user acts would re-bill the whole context for a
status ping. Results owed from before it stay owed until the next main-loop
`agent_end`. A receipt without its Python public-event
observation blocks completion and fails with a bounded diagnostic; never invent
the missing acknowledgement or delete the receipt to force readiness. Native
session admission and disk writes cannot guarantee atomic exactly-once delivery
across a process crash.

`last-notification.json` is retired and ignored. It proves no result membership
or admission; upgrading an old unconsumed terminal result can repeat a notice
whose former admission cannot be proved. Start fresh sessions when changing
bundles; never mix old and new writers for one parent.

One Python reader in `lib/harness/pi_private_state.py` supplies both CLI owed-result
discovery and this watcher with the same consumption set: admitted work IDs from
`launch-receipts.json`, CLI wait/ack/cancel/show/status/report-show observations from
`observed-launches.json`, and explicit dismissals from `cleared-launches.json`, plus their
pre-launch predecessors read as launch 1. It reads
each source once per parent and skips a damaged source independently. Both union that
set over every parent in the scope, so work consumed under a crashed generation stays
consumed after the resume. A damaged source is never empty evidence, but the two sides
fail differently: core's `owed_consumed_ids` warns once and skips it for any parent, while
the watcher fails closed on a damaged source under the current spawn (a scan fault, so
nothing publishes) and logs once and skips one under another generation, so a crashed
generation's damaged file cannot silence delivery. The watcher writes only under the
current spawn — its receipts, cleared ids, faults, and Bash records — and retains a
skipped source in its evidence ledger; wait/collect report the skip and may repeat one
header rather than fail or retry forever. With readable
state, no-argument `spawn wait` and `spawn collect` do not replay a result already
delivered or cleared.
