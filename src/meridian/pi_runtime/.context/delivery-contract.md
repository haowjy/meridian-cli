# Pi result delivery

Results are consumed per launch, not per spawn: continue retains the spawn ID.
Missing coordination files mean empty; malformed or wrong-parent authority means
unknown. Missing or unreadable task logs are unavailable, not empty output.
`observed-launches.json` is shared with Python and Claude; change its schema in
both the extension and `lib/state/spawn_observations.py`.

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
origin sidecars, and remembered scans cannot supply child authority. A candidate cache reduces idle reads but is not authority: revalidate canonical
state and the exact parent before delivery. Continued launches and past owed launches
remain independent obligations.
Before sending a bare Bash result, rediscover after terminal state: cached
absence cannot prove that a child has not taken over its obligation. Revalidate
that discovery through claim and send. Failed or superseded full discovery stays
owed; cached candidates cannot clear its fault. Timer and watcher requests do no
polling I/O while Pi is active.

A tool-bearing turn boundary and late idle fallback serialize through one owner.
Each notification has bounded members and bytes. Every included result must retain
its identity, terminal outcome and a way to retrieve full output; optional detail
cannot crowd out those fields or starve later results. Unreadable evidence fails
closed. Explicit waits use the CLI.

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

A tracked terminal result stays owed during active tool waits so a wait can
consume it before automatic publication. Queue at most one backlog message per
native opportunity; a later boundary or idle transition opens another.

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
or restart reopens collection; timer and watcher wakeups alone do not. This failure mode is not yet reproduced.

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

Python's `lib/harness/pi_private_state.py` and the watcher independently read
admission receipts, CLI observations and dismissals across all owned parent
generations. Keep their consumption semantics aligned. A damaged source is not
empty: core warns and skips it; the watcher fails closed for its current parent,
but warns and skips an older parent's damaged source so it cannot silence the
current session. Cross-generation consumption prevents replay after resume.
