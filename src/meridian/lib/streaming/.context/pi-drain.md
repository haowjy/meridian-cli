# Pi drain

Pi is idle only after agent settlement and with no open compaction. `agent_end`
is provisional: retry or compaction can follow. A new run invalidates the prior
success candidate. Queue updates and summarization-retry events are not paired
activity boundaries and cannot establish busy or idle.

Persisted, parent-linked descendants are the authority, including grandchildren
beneath a terminal or archived child. Streamed lifecycle-like messages are not
descendant evidence. Each proposed success needs a refresh begun after the
proposal; cached readiness cannot authorize it.

A missing private-work file means no blocker; an unreadable or invalid file is
unknown evidence, not an empty snapshot. `done` cannot bypass an active turn,
owed delivery or unknown evidence. Receipt persistence precedes the public event:
until the exact delivery is observed, admission remains unknown, and the parent
must subsequently become idle. Never fabricate a missing observation after a crash.

Child-wave, unknown-evidence and delivery windows are separate and anchored;
polling does not extend them. A new active turn clears the idle delivery window.
There is no default total wall-clock ceiling across successive child waves;
`--timeout` provides one when needed.

The [delivery contract](../../../pi_runtime/.context/delivery-contract.md)
describes the private disk records. KB `architecture/completion-drain.md`
describes the shared completion coordinator.
