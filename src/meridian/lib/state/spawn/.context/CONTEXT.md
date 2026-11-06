# Spawn state

A terminal writer holds the launch lock as well as the mutation lock. Read-only
status projection cannot authorize cleanup or persist the outcome. Legacy
`runner_exit` is not the authority for a dead lock-backed launch.

Quarantine preserves unrecognized data rather than treating it as absent: an
unreadable row may describe active work. See [../AGENTS.md](../AGENTS.md) for
lock order and [.context/history.md](../../.context/history.md) for history.
