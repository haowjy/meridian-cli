# pi_runtime/ — Context

Lifecycle handlers use their invocation context, never a retained context. A
shutdown context may already be invalidated by Pi session replacement or reload.
The extension recognizes only the exact error-text prefix
`This extension ctx is stale after session replacement or reload.` and publishes
that shutdown without identity, clearing any earlier quit. Other identity-read
faults poison the record and rethrow. Requalify this error-text dependency when
upgrading Pi; never infer exit from current/target session or the last-seen ID.

`managed-bash` is the mechanism extension. `meridian-spawn-watch` is the policy extension.
Keep that split: shell task execution and task record persistence belong in managed-bash;
child-spawn observation and notification behavior belong in spawn-watch.

The permissions extension never tries to understand arbitrary shell. It splits a command
into simple words, single quotes, double quotes without expansion/escaping, and the
separators `&&`, `||`, `;`, `|`, and newline, and matches each simple command. What it does
with other syntax is the policy's `bash_fail_closed`, which the Python compile sets: it
refuses that syntax before rule matching when the rule deciding unmatched commands (bash's
unscoped rule, else `*`'s) is `deny` or `ask` — every allowlist, including the unscoped
deny the policy writes beside scoped allows — or when a scoped `ask` exists. Under an
unscoped `allow` (or no rule) only scoped denies can refuse, so they are best effort: an
unsplittable command runs, and the report marks those denies `partial`. Refusing there
would break ordinary shell use (`2>&1 | tail`, `$VAR`, redirects) for every profile with
`bash: allow` and a few scoped denies. A completion
backstop applies only when the entire input is one `meridian spawn done` or `meridian
spawn rearm` command whose optional arguments contain only letters, digits, `_`, `.`,
or `-`. File-tool scopes canonicalize the requested path and the rule's non-wildcard
root against the process cwd, resolving existing symlinks. Pi follows OpenCode's
wildcard meaning — `*` can cross `/` — but canonical absolute roots prevent it from
matching a traversal or symlink escape outside the scoped root.

The managed Bash owner is launch-scoped in `globalThis`, keyed by its record
path, so extension reload preserves live process handles and rebinds current
hooks. `session_shutdown(reason=reload)` only clears UI hints. Normal shutdown
terminates all owned groups, including tasks detached from quiescence. A task
owns the POSIX group created for it, uses bounded TERM→KILL escalation, and
publishes terminal after group exit and its queued log writes. Its output queue
applies stream backpressure; failures close owned pipes before cleanup and
release outstanding operations with an error instead of an unhandled rejection.
Output progress is not persisted: `log_bytes` in the records file is the size at the
last lifecycle write (start, background, ping, terminal, consumption), live panels
read the in-memory size, and the logs are the output authority. A log write fault
still fails the task at the chunk; a records-store fault surfaces at the next
lifecycle write, at the latest terminal publication.
Ping delivery is advisory: a stale notification callback releases its durable
ping claim and warns, preserving the live shell owner. It does not retry the
same broken capability in a timer loop; hook rebind or later task activity can
schedule another attempt. Publication failure remains a task/storage failure.

Cold recovery validates and retains the complete record store before accepting
commands. Historical terminal/wait-consumed rows remain intact. Running rows
have no recoverable process handle and retain `status=running` plus
`ownership_lost` in `execution_error` (appended once after any durable fault, never
replacing it); a recorded PID never authorizes signalling.
`bash_manage(kill)` explains the lost ownership, `wait` reports it, and explicit
`detach` durably releases tracking. `runtime_error` records failed publication
when storage permits; a successful later command can repair publication. A
corrupt store is preserved and refused until repaired. Log readers bound bytes
with seek/read, and panels load preview snapshots before rendering.
Wait propagates supervised execution failures even after group cleanup; it does
not consume a failed result. Clearing history prunes returned foreground,
untracked, durably wait-consumed, or causally admitted terminal rows, preserving
unattended background completion obligations. Read admission receipts through the shared strict
reader at the owner's frozen path; invalid evidence refuses pruning.
Native `ctx.mode` determines custom-panel availability: Pi 1.0 has `hasUI=true`
in RPC, but its custom-panel API is a no-op. RPC task output uses required
`ctx.ui.notify`; print mode retains text output. Tables and logs must never be
written directly to the RPC stdout transport.

A model's foreground `bash` is backgrounded after 4 minutes (or its `timeout_min`). A task that
passed no `timeout_min` and is then running a Meridian wait or a foreground spawn re-arms the timer
and checks again at the next expiry (`meridian_hold.ts`), until it has been in the foreground 55
minutes. A backgrounded `meridian spawn wait` consumes the child's report, and the bash completion
notice carries no output, so the model would get only a truncated tail, behind an extra call.

Live `Execution` objects separately own handles, output queues, progress, creation
proposals and first-fault evidence. A snapshot swap cannot replace them. Readers
see published rows with live progress and negative fault evidence overlaid; those
facts can also ride the next lifecycle snapshot. Positive terminal results,
background/tracking changes, ping claims, consumption and removal still require
publication. Launch returns the owned execution and its separately supervised
creation observation: foreground abort attaches to the former immediately, while a
positive start still awaits the latter. Cancellation noticed after preparation I/O
but before spawn removes its empty logs and launches nothing. A missing, unreadable
or non-regular log is unavailable evidence, not valid empty output. UI inspection
stays non-consuming; `/ps` loads metadata only and owns one bounded selected-row
preview read at a time.

Terminal publication is such a transition. Until it lands the record stays
`running` and owned, so list, wait, output, kill, and foreground results never
present, wait on, or consume an unpublished clean exit; a wait interrupted before
publication still reports running. Its linearization point is staging: success is staged only if no
failure has claimed the record, so an earlier failure prevents a clean terminal
snapshot; one claimed afterwards is ordered after it and repaired by the failure's
own write (a crash inside that rename window can still recover the success).
A task-fatal stage/write failure claims its record inside the failing publication
slot, before any successor can stage. Caller promise depth cannot change that
order. Claiming returns no completion promise: cleanup/repair run separately, so
output drain and physical interruption cannot inherit a metadata wait. Detach,
consumption and clear failures instead remain caller-recoverable errors. A failed terminal write
fails the record (`killed` plus `execution_error`), returns a failed wait result,
and leaves the notice owed. A consumption is staged in its own slot and is skipped if the record
has failed meanwhile. `kill` reports the published outcome, so a task that exited by
itself is not "killed". Group-signalling authority ends where physical ownership
does: once `ShellTask` observes its group gone (including ESRCH from a signal and
ps queries already in flight) it never signals or probes it again, however long
publication takes. Interrupting
a foreground command returns after the physical stop and flushed output, not after
creation or terminal publication. A clean interruption publishes as `killed`; a
fault overlay may show negative evidence before its repair write lands. Foreground
hotkey/timeout handoff is positive only when its queued transition finds a published,
still-running row; a creation failure or terminal publication ahead of it declines
the handoff without resurrecting or backgrounding the row.
Consumption is persisted before the terminal wait or output result returns. If it
fails the marker was never applied and the wait returns an error, leaving the
completion eligible for notification.
Spawn-watch serializes publication through one launch-scoped owner. A successful
tool-bearing boundary and the genuinely late idle fallback use that owner rather
than separate delivery machinery. Automatic child notices read `report.md`
directly instead of invoking the general `spawn wait` CLI. Candidate IDs reduce
ordinary idle work but never become authority; canonical state and parent-local
evidence remain authoritative at publication. Two nonrecursive watchers and the
existing timers wake the owner; active runs do no polling I/O. Idle delivery is
armed only by a main-loop `agent_end`, never by a session start: a resumed or
reloaded lead's prompt cache is cold, so a turn nobody asked for re-bills the
whole context for a status ping. Watchers and polling may start at `session_start`;
they publish nothing until a turn ends.

See the canonical [delivery contract](delivery-contract.md) for discovery and
publication sequencing, fixed report/member/message bounds, subtract-only
revalidation, cancellation generations, exact admission/public observation, and
restart/crash behavior. Scan, report, and receipt failures remain supervised in
`delivery-fault.json`.
