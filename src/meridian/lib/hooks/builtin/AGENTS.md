# Built-in hooks

Built-ins use `meridian.plugin_api` for general capabilities. Notification transports stay
internal; notify imports its transport lazily because config loading imports built-ins for
their schemas.

`git_autosync.py` is a stub for the removed hook: a `git-autosync` row loads, does nothing
and warns. Delete it one minor release after it shipped (see `docs/releasing.md`). Syncing
lives in `crates/meridian-sync` and `lib/autosync`. See [.context/CONTEXT.md](.context/CONTEXT.md).
