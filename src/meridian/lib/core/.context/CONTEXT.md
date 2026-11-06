# Core lifecycle

Root-only cleanup fails closed on malformed depth: treating it as root could
reap work from a delegated process. A session-owned scope whose root is dead or
has a mismatched birth time is reclaimed, rather than preserved forever.

Live execution facts outrank report text on abnormal exit. Dead-launch
reconciliation has a separate rule: a durable report wins over cancel intent;
without one, cancel intent yields cancelled and a free launch lock yields a
crash. See [cancel.md](cancel.md) for cancellation.
