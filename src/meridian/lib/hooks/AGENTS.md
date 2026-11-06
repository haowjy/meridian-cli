# Hooks

Built-ins use `meridian.plugin_api` for general capabilities; their tightly
coupled companions are described in [builtin/AGENTS.md](builtin/AGENTS.md).

`meridian hooks list/check/run` resolve from the current directory, ignoring an
inherited project directory so a spawn cannot operate on another project's hooks.
Notification emission follows the session's project instead.

[.context/CONTEXT.md](.context/CONTEXT.md) describes dispatch and routing.
