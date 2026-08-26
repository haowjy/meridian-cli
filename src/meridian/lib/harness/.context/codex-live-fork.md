# Codex Live-Fork Snapshot

Codex is the only harness whose fork transcript Meridian materializes; Claude,
OpenCode, and Pi delegate fork to the harness binary. A Codex source rollout may
still be receiving an appended JSONL record while it is forked, so a raw copy to
EOF is unsafe.

`materialize_fork_rollout(...)` opens the source once, takes an `fstat` size
bound from that descriptor, and streams no more than that bound into an atomic
replacement. It publishes only complete newline-terminated JSON records,
validates every copied record, and rewrites the first `session_meta` id. An
incomplete record at the snapshot boundary is expected live-writer state and is
dropped.

Rollout publication and Codex SQLite registration form one compensated action.
If registration raises, `fork_session` reopens the database to determine whether
the row committed: a committed row wins, an unregistered rollout is removed,
and an indeterminate reconciliation preserves the rollout for recovery. Do not
replace this with an unbounded file copy or unconditional cleanup. See KB
`lessons/harness-integration.md` for the failure that established this contract.
