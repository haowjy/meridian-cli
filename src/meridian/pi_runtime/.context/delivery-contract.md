# Pi result delivery

All files below live under `pi-bash/<parent>/`. Missing files mean empty;
present malformed or wrong-parent authority means unknown, never successful empty.
Atomic writes use the shared JSON helper. One parent has one execution writer,
one launch-scoped notification owner, and one Python public-event observer.

| File | Writer | Shape and purpose |
|---|---|---|
| `bash-records.json` | managed Bash | v1 task records; tracked terminal background rows stay owed until wait/output consumption or admission. `execution_error` on a row and file-level `runtime_error` preserve unresolved execution/storage evidence. |
| `delivery-receipts.json` | spawn watcher | `{v:1,spawn_id,messages:{delivery_id:work_ids[]}}`; exact native custom-message admission, never `sendMessage()` return. |
| `delivery-observations.json` | Python | `{v:1,spawn_id,observed_message_ids:[]}`; exact public admission event observed after marking the parent active. |
| `observed-spawns.json` | CLI wait | v1 parent, durable `observed_spawn_ids`, diagnostic `waiting_spawn_ids`, `wait_reservations` keyed by caller with `owner_pid`, `owner_birth_epoch`, `expires_at_epoch`, `spawn_ids`. Only live matching process leases suppress temporarily. |
| `cleared-spawns.json` | spawn watcher UI | v1 parent, finite `updated_at_ms`, `cleared_spawn_ids`; explicit dismissal. |
| `delivery-fault.json` | spawn watcher | `{v:1,spawn_id,operation:"scan"|"admission"|null,error:string|null}`; bounded delivery diagnostics independent of execution records. |

The watcher derives children from canonical direct `parent_id` rows; an
`originating_bash_id` on those rows transfers the launcher's obligation to its
child. Logs, timers, origin sidecars, and remembered scans cannot supply child
authority. A disposable candidate-ID set scopes frequent idle reads, but each row
is reparsed and must still have the exact parent. Successful tool boundaries do a
fresh full discovery; startup/rebind requests the same discovery once idle. The
nonrecursive root watcher accelerates idle discovery, and a 30-second full
reconciliation recovers missed events or delayed state rows.
If an ordinary 500 ms idle poll already owns the single flight at reconciliation,
the next tick discovers the row, for about 30.5 seconds maximum idle-only latency.
While Pi is active, these timer and watcher requests perform no disk I/O; the next
tool boundary supplies fresh discovery.

Publication has two serialized paths: a successful tool-bearing `turn_end` awaits
a fresh scan and queues native steer before the next inference, while genuinely
late work retains the native-idle follow-up fallback. Both recheck consumption
after bounded direct `report.md` reads. Automatic delivery reads at most 64 KiB per
selected report, marks truncation with the full path, supplies status/transcript
guidance for missing or empty reports, and fails closed when present evidence is
unreadable. Explicit wait commands still use the CLI. A tracked background terminal
result stays owed during active tool waits, so a terminal wait can persist
consumption before the boundary runs. The idle path checks idleness before its
final snapshot and again before queueing, in the same generation that is invalidated
by `agent_start`, rebind and stop. The collector is finite: after report reads it
takes one final evidence snapshot and subtracts consumed, leased, claimed or
changed initial members. New
arrivals and unstable work remain owed for a later boundary or idle tick. Delivery
before the next inference is not guaranteed when an attempt cannot obtain stable,
readable evidence. One message identity
reserves the selected IDs through native admission. Its custom message contains
`details.delivery_id` and `details.work_ids`; the native `message_start` hook
persists precisely that membership before the public RPC event.

Steer joins Pi's native queue and is ordered by Pi with any other queued input; it
is not a priority channel over user steering. Native admission means the message
entered the context, not that the model acted on it. An enqueued message that Pi
never admits (for example, the queue is cleared) keeps its IDs reserved until
admission or restart; this limit predates boundary steer and is unconfirmed as a
reproduced defect.

The boundary path carries the native operation abort signal and publishes only for
completed outcomes with actual tool results. Error, aborted and non-tool turns do
not open a publication window or force continuation. Hot reload rebinds the
launch-scoped owner, invalidates stale drains and preserves queued claims. A cold
restart has no old native queue: unreceipted terminal results retry, receipts and
explicit consumption stay consumed. A receipt without its Python public-event
observation blocks completion and fails with a bounded diagnostic; never invent
the missing acknowledgement or delete the receipt to force readiness. Native
session admission and disk writes cannot guarantee atomic exactly-once delivery
across a process crash.

`last-notification.json` is retired and ignored. It proves no result membership
or admission; upgrading an old unconsumed terminal result can repeat a notice
whose former admission cannot be proved. Start fresh sessions when changing
bundles; never mix old and new writers for one parent.
