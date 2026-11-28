# Spawn state

A terminal writer holds the launch lock as well as the mutation lock. Read-only
status projection cannot authorize cleanup or persist the outcome. Legacy
`runner_exit` is not the authority for a dead lock-backed launch. Old row formats are
read in memory (`legacy.py`) and rewritten on disk once (`legacy_rows.py`; see
[CONTEXT.md](../../.context/CONTEXT.md#one-time-rewrite-of-old-spawn-rows)).

See [../AGENTS.md](../AGENTS.md) for lock order and
[history.md](../../.context/history.md) for history.
