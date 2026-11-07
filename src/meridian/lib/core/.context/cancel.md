# Cancel

Cancellation is pinned to the launch number and runner identity observed in the
stored row. A derived read can already call a dead launch terminal before its
terminal record has been written. Cancelling a live managed primary requires
`--force`; without it cancellation is refused before recording intent.

The service records cancel intent before attempting a nonblocking dead-launch claim.
It also writes the legacy row field for rows without a launch because older runners
read cancellation there. A claim checks the launch number under the lock: if a
continue has started another launch, it must not claim or signal that new launch.
Polling an older launch reads its archived outcome. Signals and teardown use the
observed process birth; an unknown birth is never signalled.

A dead launch is torn down and finalized without waiting for a signal. A durable
completion report in the launch’s `report.md` wins over cancel intent and makes
the outcome succeeded; without a completion report the intent makes it cancelled. For a live runner, cancellation sends TERM, waits, then kills the tree
and managed primary scopes if needed. If the lock remains held at the deadline,
the result is `finalizing`; intent remains available to the eventual finalizer.

Terminal writes and `spawn.finished` hooks run under a termination mask so a signal
cannot interrupt the durable outcome or its hooks. A caller with essential work after
finalization must hold an outer mask: the streaming runner must mark scopes released
and record completion before a deferred signal is delivered.
