# Hooks

`meridian hooks list/check/run` resolve from the current directory, ignoring an
inherited project directory so a spawn cannot operate on another project's hooks.
Notification emission follows the session's project instead.

A machine event (`inbox.received`) reads its rows from the user config alone. Never open
a project's config for one, and never give its detached process a descriptor of the
process that started it.

[.context/CONTEXT.md](.context/CONTEXT.md) describes dispatch and routing.
