# Built-in hooks

Capture conflict paths and type before `git merge --abort`; the index is clean
afterward. Include untracked files in the excluded-file stash so a remote file
with the same name cannot block the merge. A failed divergence check is unknown,
not zero ahead and behind; treating it as zero silently skips sync.

Use `hook_event` in structlog calls: `event` is reserved and collides with its
positional argument.
