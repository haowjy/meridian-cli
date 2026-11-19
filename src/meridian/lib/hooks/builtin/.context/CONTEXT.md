# Built-in hooks

A failed divergence check is unknown,
not zero ahead and behind; treating it as zero silently skips sync.

Use `hook_event` in structlog calls: `event` is reserved and collides with its
positional argument.

Never SIGKILL git: a killed git leaves `.git/index.lock` and every later sync fails.
`_run_timed` runs git in its own process group and SIGTERMs the group on timeout.
Every sync outcome is written to the state file by `execute`, keyed by clone root;
`state.outcome` is the skip reason or `success`, and `meridian context` shows
anything but `success`/`nothing_to_sync` as `failed`. With a remote, sync stops
(`off_branch`) unless HEAD is on the branch `origin/HEAD` names.
