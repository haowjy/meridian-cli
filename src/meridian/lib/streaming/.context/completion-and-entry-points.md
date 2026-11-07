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

`hold_state.py` is the hold's whole lifecycle: one frozen `HoldState`, `decide(state, facts)`
and the folds and readers around them, all pure. `_HoldProfile` reads the signal files and
the clock, sends, and applies what they return. The rows live in `decide`, and
`tests/unit/streaming/test_hold_state.py` pins them; this page says why they are that way.
An **ask** is any question the hold sends. Its kind is expiry if `answer` is open when the
send starts, otherwise turn-end. "Nudge" stays the wire word (`source="completion_nudge"`).

`decide` runs its timers first: a `SENT` ask past its grace becomes `SETTLED`, and a
`SpacedLease` past `not_before` retires. So neither can stay a wake, and `next_wake` needs
no clock. A timer the shell has to guard with the clock is how the 1 ms spin got in. Then the
first matching row wins, and success turn ends and wait evaluations share every row from
B1 down, which is why a hold behaviour has one place to live.

**Success never publishes over a running turn.** A candidate stands for a finished turn, so
a turn running at a close outdates it. A2, D3, E2 and `outcome_at_close` all read
`active_turn`. A transport close during a turn takes its own row, A3b, which fails with the
close's outcome and cancels descendants, and ends the hold there: no later evaluation can see evidence
come back and publish the old reply. For the same reason `_active_turn` stays set at a failed
terminal event: clearing it first would blind those rows. A candidate at an idle child stays
good, so a close with a ready tree publishes it. A `spawn cancel` of an idle child usually
ends `cancelled` first, through the runner's SIGTERM path; `outcome_at_close` decides only
when the drain reads the close first, and whether that should stay `succeeded` is an open
owner call.

**A rearm is charged only when `decide` takes it.** `after_signals` reads the budget and row
B2 spends it, so a rearm dropped by a row above B2 costs nothing.

**A `closing` child in a turn falls through to expiry,** which is why a wedged `done` is asked
and not published.

`after_send` runs once on every outcome, including failures, and stamps `asked_at` when the
attempt ends, so the grace starts when the send ends. Only cancellation skips it, and that
stops the coordinator. A refused send still counts as an attempt for the 270 s spacing.

| Outcome of `deliver` | Means | `ask` | `answer` | Then |
|---|---|---|---|---|
| Returned | Delivered | `SENT` | `retry_left = False` | |
| `DeliveryRefused` | Alive, nothing written | Expiry with nothing asked: `REFUSED`. Else `SETTLED` | unchanged | Raise |
| `TransportDead`, or `backend_dead()` | Gone | `SETTLED` | `retry_left = False` | Raise |
| Any other exception | May have arrived | As returned | As returned | Log |

The raise reaches the coordinator's `except` around `send_nudge`, which re-evaluates at
once: that is how a ready tree publishes after a failed ask, and how B1 fires. An unknown
send counts as sent because Claude's normal success is a write with no receipt, so "sent"
already means "may have been dropped", and resending could put the same nudge in front of
the child twice.

A dead transport reaches the hold as a close event, usually before B1 can see it. With no
turn running and a ready tree, A2 publishes, even with an expiry ask out: a Codex child
SIGKILLed while idle ends `succeeded`. With a turn running it ends `failed` with the close's
message, as above. OpenCode differs: a SIGKILLed server surfaces as a read error that fails
the drain with no hold row involved, idle or not. Blocked or unknown evidence waits in A4, and then `defers_close` and
`outcome_at_close` decide. B1 covers a backend found dead with no close event yet, so it fires
at the next turn end or expiry. A turn-end ask has no window: a ready tree publishes, and
otherwise the stream close or the next expiry ends it. Row A4 and `defers_close` hold for
descendants on purpose, so B1 must not widen to turn-end asks.

An ask that goes unanswered ends at the next expiry only if `answer` has no retry left. A
blocked turn-end ask opens a window at the first expiry and cleans up 300 s later. An expiry
ask that was sent has no retry, so its next expiry cleans up.

The drain blocks during a send. One send is bounded at 33 s, but the wait for `run_action`'s
lock is not: it is first in, first out, so every queued `spawn message` adds a send, and a
stop with no budget waits behind them. Nothing deadlocks: the drain holds no lock while it
waits, and the Codex and OpenCode event queues are unbounded, so their reply is still read.

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
- `hold_state.py` — the hold's state value, `decide` table, folds and close rules; pure
- `resident_drain.py` — `HoldEvidence`, `_HoldProfile` (the shell around `hold_state.py`),
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
