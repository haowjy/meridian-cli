# Streaming runtime

## Completion evidence

A hold combines native task evidence with persisted descendants; an unreadable descendant
assessment is `unknown`, not an empty tree. Pi uses the same transitive descendant
assessment. Archived rows remain traversal edges so a live descendant below an archived
intermediate is found. See [state history](../../state/.context/history.md) for the
projection contract.

A proposed coordinated success requests a descendant refresh; success needs a refresh
started after that request, not cached readiness. Stabilization and fresh-evidence validation are separate phases,
and activity interrupts either. This narrows but does not eliminate the race with admission
of a new child. After event EOF, completion decisions and evidence refreshes continue while the stream
close is deferred, so an unvalidated candidate cannot publish early.

The launch-scoped `accepted-signals.json` is written before the drain starts. The CLI
refuses a signal missing from a valid file. If the file is absent or unreadable, the CLI
writes the signal but cannot confirm the runner reads it; this is “delivery unconfirmed”. Keep it off
the spawn row: older readers quarantine rows with unknown fields but ignore extra files.

## Report and publication

Finalization prefers a child-written `report.md`, then captured completion text, then later
assistant text. A native-task hold captures each successful turn so the report follows the
work, even if a brief wake replaces a fuller report. A hold without a native source keeps
its candidate text through an ordinary confirmation, so “Done.” does not replace the
report. A [wake or an expiry ask](completion-and-entry-points.md) sent between turns instead
makes the answer the report;
keep nudge wording aligned with the capture rule. Rearm retains the previous text until a
new candidate replaces it. Pi drops the text with its candidate.

Terminal outcome precedence is drain success, then an authoritative stop, then the
remaining drain outcome. Publication precedes best-effort teardown, so slow or failed cleanup cannot
rewrite the outcome. A stop waits for its own cleanup; shutdown drains the remaining tasks.
Deadlines use process-local monotonic time and do not survive a crash.

## Control socket

The bounded per-user socket path avoids `sockaddr_un` path limits. A stale-socket sweep
uses a datagram probe and checks file identity before unlinking: a stream probe can refuse
a live listener with a full backlog. Uncertain errors and files belonging to other users
are left alone. The spawn's [launch lock](../../state/launch_lock.py) prevents another
runner of that spawn from binding the same endpoint while the owner is live.

The socket accepts one newline-delimited JSON `user_message` request per connection. A
successful response distinguishes `posted` (written, no receipt) from `acknowledged`
(harness receipt); failures say when delivery may have occurred. A caller must not retry an
ambiguous delivery blindly. The request limit is 16 MiB and the client waits at least 35 s
for the bounded send.
