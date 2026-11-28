# Spawn lifecycle

Root-only cleanup fails closed on malformed depth: treating it as root could
reap work from a delegated process. A session-owned scope whose root is dead or
has a mismatched birth time is reclaimed, rather than preserved forever.

Live execution facts outrank report text on abnormal exit. Dead-launch
reconciliation has a separate rule: a durable report wins over cancel intent;
without one, cancel intent yields cancelled and a free launch lock yields a
crash. See [cancel.md](cancel.md) for cancellation.

## Two orders for ending a dead launch

`finalize_dead_launch` (reaper, cancel, doctor) runs its `before_finalize` sweep first, then
writes the outcome and dispatches `spawn.finished`. `finish_last_launch` (a continue, holding
the launch lock it will reopen under) writes the outcome and dispatches `spawn.finished`
first, then resolves the cleanup claim, stops a managed primary's backend and TUI, and sweeps
the scopes, refusing the continue when something survives. Git history doesn't say why the
continue path differs (it arrived whole in "A continue keeps the spawn ID"). What the code
shows: a continue that refuses at the sweep has still recorded the last launch's outcome.
Unifying the two orders changes when hooks see `spawn.finished` relative to process teardown,
which is a behavior change to decide on its own.
