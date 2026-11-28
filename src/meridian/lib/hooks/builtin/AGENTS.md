# Built-in hooks

Built-ins use `meridian.plugin_api` for general capabilities. Notification transports stay
internal; notify imports its transport lazily because config loading imports built-ins for
their schemas. Syncing git contexts is not a hook: it lives in `crates/meridian-sync` and
`lib/autosync`. See [.context/CONTEXT.md](.context/CONTEXT.md).
