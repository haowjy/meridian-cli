# Built-in hooks

Use `hook_event` in structlog calls: `event` is reserved and collides with its
positional argument.

The `git-autosync` stub keeps its old default events and `remote`/`path` identity options, so
an old row still gets a distinct name and loads without error. It validates nothing in
`options`: a row written for the old hook must keep loading.
