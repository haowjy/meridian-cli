# Pi result delivery

All files below live under `pi-bash/<parent>/`. Missing files mean empty;
present malformed or wrong-parent authority means unknown, never successful empty.
Atomic writes use the shared JSON helper. One parent has one execution writer,
one launch-scoped notification owner, and one Python public-event observer.

| File | Writer | Shape and purpose |
|---|---|---|
| `bash-records.json` | managed Bash | v1 task records; tracked terminal background rows stay owed until wait consumption or admission. `execution_error` on a row and file-level `runtime_error` preserve unresolved execution/storage evidence. |
| `delivery-receipts.json` | spawn watcher | `{v:1,spawn_id,messages:{delivery_id:work_ids[]}}`; exact native custom-message admission, never `sendMessage()` return. |
| `delivery-observations.json` | Python | `{v:1,spawn_id,observed_message_ids:[]}`; exact public admission event observed after marking the parent active. |
| `observed-spawns.json` | CLI wait | v1 parent, durable `observed_spawn_ids`, diagnostic `waiting_spawn_ids`, `wait_reservations` keyed by caller with `owner_pid`, `owner_birth_epoch`, `expires_at_epoch`, `spawn_ids`. Only live matching process leases suppress temporarily. |
| `cleared-spawns.json` | spawn watcher UI | v1 parent, finite `updated_at_ms`, `cleared_spawn_ids`; explicit dismissal. |
| `delivery-fault.json` | spawn watcher | `{v:1,spawn_id,operation:"scan"|"admission"|null,error:string|null}`; bounded delivery diagnostics independent of execution records. |

The watcher derives children from canonical direct `parent_id` rows; an
`originating_bash_id` on those rows transfers the launcher's obligation to its
child. Logs, timers, origin sidecars, and remembered scans cannot supply child
authority. Polling guarantees child-file changes are eventually read even when
nonrecursive directory notifications miss them. Event bursts do not reset the
first scheduled scan.

Publication waits for native `ctx.isIdle()` and rechecks consumption after any
formatter await. A tracked background terminal result stays owed during active
tool waits, so a terminal wait can consume it before a follow-up enters the
native queue. Partial consumption rebuilds the batch. One message identity
reserves the selected IDs through native admission. Its custom message contains
`details.delivery_id` and `details.work_ids`; the native `message_start` hook
persists precisely that membership before the public RPC event.

Hot reload rebinds the launch-scoped owner and preserves queued claims. A cold
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
