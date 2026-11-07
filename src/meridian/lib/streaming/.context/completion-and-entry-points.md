# Hold completion

A **candidate** is a finished turn's outcome awaiting publication; retained report text
is separate. An **ask** is a completion nudge; its **answer window** gives an expiry ask
300 seconds for work. An ask is **turn-end** kind outside that window and **expiry** kind
inside it. A **rearm** renews the idle limit and holds completion until an ask; it can
drop the candidate without deleting retained report text. A **lease** delays that ask
while its blockers remain or its spacing or idle window lasts.

Hold completion uses an idle limit, renewed by `rearm` and work inside a turn. Turn starts,
Meridian nudges, heartbeats and background activity do not renew it: otherwise a nudge or a
chatty idle child could keep itself alive forever. A silent command lasting the whole window
is idle. Pi instead waits for descendant quiescence without a default total wall-clock bound;
its child-wave windows do not slide with activity. `--timeout` / `MERIDIAN_TIMEOUT` is a
separate opt-in absolute ceiling that neither hold work nor Pi waves reset.

The hold selects behavior by capability. A native task source reports its own work, so a
normal turn end needs no confirmation; without one, a writable signal channel allows a
confirmation ask. A hold without a writable channel skips confirmation but still checks
evidence and deadlines. Rearms are unlimited by default; the configured budget limits only
rearms granted, and the count persists.

## Why the table is ordered this way

The pure table and its folds are in `hold_state.py`; `tests/unit/streaming/test_hold_state.py`
pins the rows. Timers retire before decisions, so expired timers cannot cause repeated
immediate wakes.

A running turn outdates a candidate. A transport close during that turn fails and cleans up
immediately: later evidence must not publish the old reply. A close with a ready tree and
no active turn can publish the candidate. An idle `spawn cancel` normally reaches the
runner's cancellation path first. If the drain reads a ready close first, it proposes
success.

A first turn end has unknown evidence until the refresh completes. When a native hold clears
at a turn end, it settles briefly before publication: a queued native wake may start a turn
after evaluation. Claude's `system/init` marks that turn active before assistant output,
without renewing the idle clock. Activity cancels settlement; validation must not settle a
second time.

Rearm holds completion until an ask ends that hold, even on a native source; otherwise
invisible work would close at the next turn end. Visible blockers form a lease until they
change or clear; compare blockers,
not generations, because Claude changes generation on each result. An invisible wait uses
spacing from the last turn end, or the idle limit with `--long`. Spacing does not expire or
schedule a past-due wake during a turn, which would spin the drain. When visible blockers
appear, they become the lease because their clearance is observable. Native blockers remain
visible even when descendant evidence is unknown, so a rearm can lease them.

A rearm consumed at turn end drops the candidate, while retaining the previous report
text. If the lease ends before another candidate exists, the ask must be expiry-kind: an unanswered turn-end ask would otherwise wait until
the idle limit. A rearm costs budget only when the table takes it; a higher-priority outcome
must not spend a grant.

An expiry ask sent between turns promises that the reply is the report and closes over
remaining work. Set that promise after a successful or ambiguous send, not at decision time: only a send made while
no turn is running qualifies, and work in the answering turn closes the answer window first.
There is no reply ID: after a send between turns, the next turn end is treated as the
reply. The drain waits for the serialized send; a send during a turn does not make that
turn its closing reply. Rearm at the same turn end takes precedence over the reply close.
A failed reply retains its failure and the previous report; an expiry reply also cleans up because the ask promised to
close. Explicit `done` likewise cleans up remaining work but waits for an active turn and
cannot override unknown evidence. An active turn that never ends still reaches expiry.

Signals can be read without a candidate: the mailbox is read at turn ends and evaluations,
so a signal is not reserved to pre-accept a future candidate. If `done` and `rearm` appear
in one read, the later file timestamp wins (a tie favors `rearm`); `done` waits for the turn that wrote it.

Wake a parent for unread child results, not merely for a blocker clearing: a child can finish
while the parent is in a turn and never appear as a blocker. Wake only on a ready tree and
name each owed launch once. A `done` takes precedence over a wake. A wake requests new work,
so its answer replaces the report. Every expiry ask also names unwoken owed results: its
reply may close over remaining blockers before a later wake is possible. Mark those results
woken only after the ask is delivered, so a refused ask cannot hide them. A regular wake is
marked before sending, so a failed send cannot trigger an immediate retry loop.

The send grace (`ASK_GRACE_SECONDS`, 30 s for every harness, independent of the poll) starts when
the attempt ends; a turn start ends it at once. OpenCode marks a turn active only at its first
output, so a slow model needs more than a poll interval. An unknown delivery counts as sent: some
harnesses provide no receipt even on success, and retrying could duplicate the message. A
refusal can be retried once at the next expiry. An unanswered expiry leads to cleanup
after its 300 s answer window; work inside the turn closes that window.

| Outcome of `deliver` | Means | `ask` | `answer` | Then |
|---|---|---|---|---|
| Returned | Send did not fail; without a receipt this is `posted`, not proof of delivery | `SENT` | If open, `retry_left = False` | With an open window and no turn running, `reply_closes` |
| `DeliveryRefused` | Alive, nothing written | Expiry with nothing asked: `REFUSED`. Else `SETTLED` | unchanged | Raise |
| `TransportDead`, or `backend_dead()` | Gone | `SETTLED` | If open, `retry_left = False` | Raise |
| Any other exception | May have arrived | `SENT` | If open, `retry_left = False` | Log; with no turn running and an open window, `reply_closes` |

A raised send failure causes immediate reevaluation, allowing a ready tree to publish. A
turn-end ask has no answer window: a ready tree can publish, while blocked descendants wait
for close or expiry. A known dead backend with an open answer window times out and cleans
up on reevaluation. If death is not detected, an unanswered expiry waits for that window
to expire.

The drain waits for a send, including its place in the serialized action queue. A user
message to a held child is neither work nor an ask; only the resulting turn's work renews
the limit. Otherwise repeated messages could keep a wedged child alive indefinitely.

`hold.json` is an advisory between-turn checkpoint; `rearms.jsonl` records grants that took
effect, not requests. They are side files because spawn rows reject unknown fields.
