# Process scope

A process-group signal reaches reparented group members, unlike a single-PID
signal. Descendants that created their own group need the separately captured
tree; the tree fallback alone can miss children reparented during its snapshot.
`pgid_reachable` is diagnostic only: signal 0 counts zombies and has no group
birth-time guard.

The gate's wait task sends `stop` on observed child exit; cancellation before
exit closes the pipe as runner death. POSIX termination runs off the event loop
so the watcher can reap the child and disarm the guardian during the grace wait.

Signals gate on `check_root_identity`: verified, missing, reused or unknown.
Only verified (or a missing POSIX group leader, where just group members are
touched) allows a signal; an unknown birth is skipped, except on Windows, where
`terminate_job` skips only a reused PID and the job handle is the authority.
A skipped scope is `resolved == False` for `birth_unknown` and
`termination_exception`: every caller keeps such a scope tracked instead of
releasing it, and `continue` refuses with the `--fork` hint. `fallback.py` is the one
tree termination: the async form runs the sync one in a thread, and the POSIX
adapter hands off to it when the root isn't the group leader.
