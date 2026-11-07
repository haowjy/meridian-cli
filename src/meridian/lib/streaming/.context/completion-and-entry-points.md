# Hold completion

A **candidate** is a finished turn's outcome awaiting publication; retained report
text is separate. An **ask** is a completion nudge. An expiry ask opens a 300-second
answer window; a turn-end ask has none. A **rearm** renews the idle limit and
holds completion until an ask. A **lease** delays that ask while blockers remain
or its spacing or idle window lasts.

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

The decision table and folds live in `hold_state.py`. Timers retire before
decisions so expired timers cannot cause repeated immediate wakes.

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

A rearm at turn end drops the candidate but retains report text. If its lease
ends before another candidate exists, the ask must be expiry-kind: a turn-end ask
would otherwise wait until the idle limit.

An expiry ask sent between turns promises that the reply is the report and
closes over remaining work. Only a successful or ambiguous send makes that
promise; a send during a turn does not. Work in the answering turn closes the
answer window first. Rearm takes precedence over reply close, and an expiry
reply cleans up even if it fails. Explicit `done` also cleans up, but waits for
an active turn and cannot override unknown evidence.

Signals can be read without a candidate: the mailbox is read at turn ends and evaluations,
so a signal is not reserved to pre-accept a future candidate. If `done` and `rearm` appear
in one read, the later file timestamp wins (a tie favors `rearm`); `done` waits for the turn that wrote it.

Wake a parent for unread child results, not merely for a blocker clearing: a child can finish
while the parent is in a turn and never appear as a blocker. Wake only on a ready tree and
name each owed launch once. A `done` takes precedence over a wake. A wake requests new work,
so its answer replaces the report; an answer with no text leaves it. Every expiry ask also names unwoken owed results: its
reply may close over remaining blockers before a later wake is possible. Mark results woken
only after the ask or wake is delivered, so a refusal cannot hide them. A failed send
re-evaluates at once, so a refusal must not repeat there: the hold sends nothing more until a
turn end or the next expiry, and does not publish while a result is unnamed. An expiry ask
refused twice ends in the idle cleanup.

The send grace allows a slow harness time to start a turn; OpenCode marks a turn
active only at first output. An unknown delivery counts as sent because retrying
could duplicate it. A refusal proves nothing was written and can be retried at the
next expiry; transport death proves the backend is gone.

The drain waits for a send, including its place in the serialized action queue. A user
message to a held child is neither work nor an ask; only the resulting turn's work renews
the limit. Otherwise repeated messages could keep a wedged child alive indefinitely.

`hold.json` is an advisory between-turn checkpoint; `rearms.jsonl` records grants that took
effect, not requests. They are side files because spawn rows reject unknown fields.
