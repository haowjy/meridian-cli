# Process scope

A process-group signal reaches reparented group members, unlike a single-PID
signal. Descendants that created their own group need the separately captured
tree; the tree fallback alone can miss children reparented during its snapshot.
`pgid_reachable` is diagnostic only: signal 0 counts zombies and has no group
birth-time guard.

The gate's wait task sends `stop` on observed child exit; cancellation before
exit closes the pipe as runner death. POSIX termination runs off the event loop
so the watcher can reap the child and disarm the guardian during the grace wait.
