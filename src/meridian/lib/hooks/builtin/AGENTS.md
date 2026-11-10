# Built-in hooks

Built-ins use `meridian.plugin_api` for general capabilities. The tightly coupled
`autosync_store` and notification transports stay internal; notify imports its
transport lazily because config loading imports built-ins for their schemas.

Capture merge conflict paths and type before aborting: abort clears the index.
Keep untracked files in the excluded-file stash or a remote addition can block
merge. See [.context/CONTEXT.md](.context/CONTEXT.md).
