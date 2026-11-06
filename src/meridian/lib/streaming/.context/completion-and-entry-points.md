# Completion models, DrainPlan, module inventory

Moved from `AGENTS.md`; look up when changing completion, drain plans or the module list. Generic drain runtime: [CONTEXT.md](CONTEXT.md).

### Completion and Idle-Limit Models

Hold completion uses an idle limit. The window arms at the first turn end and renews on
an explicit `rearm` and on a harness event whose semantics set `work` while a turn is
active. `work` is separate from `activity`: Codex `turn/started` is a turn start but not
work, because a Meridian nudge starts a turn too, and Codex's echo of the nudge
(`item/started` with a `userMessage` item) is filtered. Heartbeats, usage and rate-limit
updates and Claude's background Agent frames are unmarked. The child's own items, deltas
and tool or shell progress are work and also answer an outstanding expiry nudge. Work
counts only inside a turn (`CompletionState.active_turn`): output from a background
process or a late item after the turn ended renews nothing, so a chatty idle child is
still nudged and cleaned up. A single command that stays silent for a whole window is
still idle. Descendant activity renews nothing.
After a rearm, no nudge goes out while the blockers the rearm answered are unchanged. If
no blocker was visible (OpenCode and Codex background shells), the next nudge waits
`COMPLETION_NUDGE_INTERVAL_SECONDS` past the previous one. A Codex nudge sent mid-turn goes through `turn/steer` (`turn/start` when idle, and when the
steer races a turn end). A nudge taken into a running turn starts no new turn, so its
answer becomes the report; with no candidate kept, the wording says so. A refused expiry
nudge is retried once at the next expiry, then the hold cleans up; on a dead backend, or
a transport close at expiry with no ready tree, it cleans up without nudging. A `done`
waits out an active turn but a turn idle for the whole window is nudged once, not
published as success.
Rearm grants are unlimited by default and the budget caps only rearms. `--resident-rearm-budget`,
`MERIDIAN_RESIDENT_REARM_BUDGET`, profile
`harness-overrides.meridian-cli.resident-rearm-budget`, or
`timeouts.resident_rearm_budget` opts into a maximum number of granted extensions.
The running grant count is persisted as `state.json`'s `resident_rearm_count`.

Pi completion is descendant-quiescence-driven and intentionally has no default total
wall-clock bound. Child-wave windows are anchored to each wave, not slid by ordinary
activity; successive legitimate waves may establish new windows. This
unbounded-while-descendants-live behavior is by design.

Explicit done authorizes completion
with leftover native tasks; managed-scope teardown stops them. With a writable
completion-signal channel, a successful candidate gets one best-effort confirmation
turn: `done` accepts, `rearm` opens another window and waits for a genuinely new
successful/idle candidate, and silence accepts only after evidence is ready. A rearm that
saw blockers snapshots them: later turn ends are not nudged while the outstanding blockers
are all in the snapshot (Claude's own `awaiting_followup_result` follow-up does not count
as a change), and a new blocker or a clear nudges again. A rearm with no blockers
follows the 270 s spacing above. Compare blockers, not `generation`: Claude bumps it on
every `result`. At expiry a hold that accepts signals sends one nudge and waits
`_IDLE_NUDGE_ANSWER_SECONDS` (300 s) for the child to work, then cleans up; a hold without
signals cleans up at expiry. A hold
without that channel skips confirmation and reads no signals, but keeps the same
evidence, blocker, and deadline policy. This no-signal path is live for read-only Codex
without spawn capability (no `subagents`, e.g. explorer), and for spawn-capable read-only
Codex when Meridian could not write its execpolicy rules file. The default completion window
is the calling harness's configured wait-yield interval unless
`timeouts.resident_deadline_seconds` overrides it.

For both profiles, `--timeout` / `MERIDIAN_TIMEOUT` is the shared opt-in absolute
ceiling. It arms the non-renewing outer attempt timer in `streaming_runner.py` and
defaults to `None`; resident rearms and Pi waves cannot reset it.

### The hold's table

`hold_state.py` is the hold's whole lifecycle: one frozen `HoldState` and `decide(state,
facts)`. An **ask** is any question the hold sends. Its kind is expiry if `answer` is open
when the send starts, otherwise turn-end. "Nudge" stays the wire word
(`source="completion_nudge"`).

| `HoldState` | Meaning |
|---|---|
| `ask` | `UNASKED`; `SENT`, a turn should start within the grace; `SETTLED`, a turn started, the grace passed, or the send failed |
| `asked_at` | Last send. The grace is `asked_at` plus the poll interval. It survives a rearm |
| `answer` | Open from an expiry ask until the child works. `retry_left` allows one more ask after a refusal |
| `lease` | `HeldLease(blockers)`: turn ends stay quiet while these are all that runs. `SpacedLease(not_before)`: rearm with nothing visible |
| `rearmed` | The next good turn end is the next candidate |
| `closing` | `done` latched |

`decide` first turns a `SENT` ask past its grace into `SETTLED`, so a grace timer cannot go
past due. Then the first matching row wins. Success turn ends and wait evaluations share
every row from B1 down.

| # | When | Decision | State |
|---|---|---|---|
| A1 | `end`, action does not terminate | `clear` | reset |
| A2 | `end` not succeeded, replaceable transport close, candidate, `ready` | `complete` candidate | |
| A3 | Same, not `ready`, `expired` | timeout | reset |
| A4 | Same, `unknown` or blocked by a persisted descendant | wait | an ask out becomes `SETTLED` |
| A5 | `end` not succeeded, anything else | `fail` its outcome | reset |
| B1 | `answer` open and `dead` | timeout | reset |
| B2 | `rearm` | `abandon_candidate`, reset deadline | keep `asked_at`, `rearmed`, new lease |
| C1 | No candidate, `expired` | expiry | |
| C2 | No candidate | wait | |
| D1 | `closing`, `unknown`, `expired`, no `end` | `fail` `resident_evidence_unreadable` | reset |
| D2 | `closing`, `unknown` | wait | |
| D3 | `closing`, no `active_turn` | `complete` candidate | |
| E1 | `expired` | expiry | |
| E2 | `active_turn` | wait | at an `end`, from here down: `rearmed = False` |
| E3 | `unknown` | wait | |
| E4 | `accepts_signals`, `UNASKED`, not `rearmed`, (`answer` closed or `end`), lease not quiet | wait + nudge | |
| E5 | `ready`, settled | `complete` candidate | |
| E6 | Otherwise | wait | |

**Expiry:** with signals, a live backend, and `answer` closed or `retry_left`, wait + nudge
and reset the deadline. Otherwise timeout. **Settled:** no signals, or `SETTLED`, or
`UNASKED` with `answer` open (the expiry ask was refused, so a ready tree may publish).
A `closing` child in a turn falls through to E1 and E2, which is why a wedged `done` is
asked and not published.

`send_nudge` sets `SENT` before it awaits, so no failure can leave the ask looking unsent
and re-fire E4, and it stamps `asked_at` again afterwards, so the grace starts when the
send ends. Then it folds the result:

| Outcome of `deliver` | Means | `ask` | `answer` | Then |
|---|---|---|---|---|
| Returned | Delivered | `SENT` | `retry_left = False` | |
| `DeliveryRefused` | Alive, nothing written | Expiry from `UNASKED`: `UNASKED`. Else `SETTLED` | unchanged | Raise |
| `TransportDead`, or health says dead | Gone | `SETTLED` | `retry_left = False` | Raise |
| Any other exception | May have arrived | As returned | As returned | Log |

The raise reaches the coordinator's `except` around `send_nudge`, which re-evaluates at
once: that is how a ready tree publishes after a failed ask, and how B1 fires. An unknown
send counts as sent because Claude's normal success is a write with no receipt, so "sent"
already means "may have been dropped", and resending could put the same nudge in front of
the child twice.

A dead transport cleans up at once for an expiry ask, through B1. A turn-end ask has no
window: a ready tree publishes, and otherwise the stream close or the next expiry ends it.
Row A4 and `should_defer_close` hold for descendants on purpose, so B1 must not widen to
turn-end asks.

The drain blocks during a send. One send is bounded at 33 s, but the wait for `run_action`'s
lock is not, so a queued `spawn message` can add one more send. Nothing deadlocks: the drain
holds no lock while it waits, and the Codex and OpenCode event queues are unbounded, so
their reply is still read.

A `spawn message` to a held child is neither work nor an ask. The hold does not see it.
What follows is an ordinary turn: `turn_active` settles an ask, the child's work renews the
clock and closes `answer`, and the turn end runs the table. Do not give it a window: a
parent polling a wedged child would hold it open forever.

## DrainPlan / DrainPolicy

`DrainPlan` carries the selected coordinator, policy, aux wake, finalizer, and async
session teardown for one active spawn. Its `coordinator=None` value is the plain path.
`accepted_signals` names the `spawn done`/`rearm` signals the coordinator consumes:
hold plans with a writable completion-signal channel `{done, rearm}`, holds without one
none, Pi `{done}`, plain none. The hold coordinator derives and exposes its set from the
connection capability; the factory copies it into the plan and sets the Pi/plain sets
directly. `SpawnManager.start_spawn` writes the plan's set once to
`spawns/<id>/accepted-signals.json` before the drain starts, and the CLI refuses a
signal the file doesn't list. Don't move it onto the spawn row (see [`CONTEXT.md`](CONTEXT.md)).
`DrainPolicy` governs terminal event behavior:

| Policy | On terminal event | Action |
|---|---|---|
| `SingleTurnDrainPolicy` (default) | any | terminate |
| `PersistentDrainPolicy` | `succeeded` | emit `meridian/turn_completed`, continue |
| `PersistentDrainPolicy` | error/cancel | terminate |

Pass `PersistentDrainPolicy` to `start_spawn(drain_policy=...)` for long-lived
session drains that keep the harness alive across turns.

## Entry Points

- `spawn_manager.py` — `SpawnManager`: public live-spawn registry/control API
- `spawn_dispatch.py` — connection creation/start dispatch
- `spawn_drain_loop.py` — event drain, persistence/observer/fan-out ordering, outcome classification, `resolve_terminal_outcome` priority resolver
- `drain_coordinator.py` — `DrainCoordinator` protocol seam for harness-specific completion policy
- `drain_plan_factory.py` — plain/resident/Pi plan selection and capability wiring
- `drain_teardown.py` — harness-neutral plan-owned connection-stop contract and default
- `pi_drain_teardown.py` — Pi connection-stop cleanup-phase policy
- `completion_contracts.py` — typed evidence, profile, and cleanup collaborator contracts
- `completion_coordinator.py` — shared candidate/wait/deadline state machine, with
  distinct stabilization and fresh-evidence validation phases
- `descendant_evidence.py` — shared reconciled, transitive persisted-descendant assessment
- `spawn_session.py` — `SpawnSession`, `DrainOutcome`
- `pi_completion_profile.py` — Pi precedence, phases, deadlines, nudges, and stream-exit
  policy
- `pi_drain.py` — Pi evidence/cleanup collaborators and drain-protocol adapter
- `pi_work_ledger.py` — sole mutable owner of Pi-private disk blockers; exposes
  immutable categorized snapshots
- `hold_state.py` — the hold's state value and `decide` table; pure
- `resident_drain.py` — `HoldEvidence`, `_HoldProfile` (the shell around `decide`),
  cleanup, and the hold coordinator
- `rearm_window.py` — shared resident/native idle-limit arithmetic and persisted rearm budget
- `pi_lifecycle_tracker.py` — validation/deduplication for produced Pi lifecycle events
- `disk_watcher.py` / `pi_quiescence.py` — validated Pi-private task/receipt disk
  observation and exact public-message admission fencing; consumption uses the shared
  `harness/pi_private_state.py` reader, and its per-source failures feed the ledger
- `drain_wait.py` — generic event/timeout/aux-wake arbitration for drain loops
- `drain_policy.py` — `DrainPolicy`, `SingleTurnDrainPolicy`, `PersistentDrainPolicy`
- `control_socket.py` — per-spawn message endpoint

Codex/OpenCode and Pi completion use the shared reconciled transitive spawn-tree assessment as
their sole persisted-descendant authority. The derived rows also decide Pi result
publication: a dead child with no written outcome reads terminal and waits on result
delivery. `pi_result_publication_pending` remains only for a row that vanishes between
the index read and the authority read. Pi's disk watcher observes private task, consumption, receipt,
public-observation and fault files; incomplete or wrong-parent spawn directories are not descendant
evidence. Accessors read one immutable cached assessment; a single-flight worker catches
up the history index, selects the transitive subtree (including archived ancestry), and
authoritatively rereads selected loose rows. Refresh completion uses the auxiliary-wake
path. Every successful publication attempt requires a refresh that started after that
attempt. A closed event stream disables further event reads but remains in the same
auxiliary-wake/timeout arbitration until that attempt is accepted or rejected. This
freshness check is not an atomic child-admission barrier.
