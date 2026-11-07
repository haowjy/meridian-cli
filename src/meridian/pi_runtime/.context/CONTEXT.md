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
unscoped rule, else `*`'s) is `deny` or `ask` — every allowlist, including an explicit
`bash: deny` beside scoped allows — or when a scoped `ask` exists. Under an
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

Live executions own process handles and fault evidence independently of the
published record snapshot. Terminal success is staged only if no failure has
claimed the record; a failure claimed later needs its own repair write, and a
crash between writes can still recover the staged success. Do not wait for
metadata publication before physically interrupting a process or draining output.
Once the owned group is gone, never signal or probe it again: its identity may
be reused.

A result stays owed until consumption is persisted. Failed publication or an
unavailable log must not turn it into a clean, consumed result. UI inspection
is non-consuming.

See the canonical [delivery contract](delivery-contract.md) for discovery and
publication sequencing, fixed report/member/message bounds, subtract-only
revalidation, cancellation generations, exact admission/public observation, and
restart/crash behavior. Scan, report, and receipt failures remain supervised in
`delivery-fault.json`.
