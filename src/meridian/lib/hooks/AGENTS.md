# Hooks

`meridian hooks list/check/run` resolve from the current directory, ignoring an
inherited project directory so a spawn cannot operate on another project's hooks.
Notification emission follows the session's project instead.

A machine event (`inbox.received`) reads its rows from the user config alone. Never open
a project's config for one. Never give its detached process the standard input, output
or error of the command that filed the message: a program reading that command's output
to end of file would wait for every row, holding whatever lock it holds.

[.context/CONTEXT.md](.context/CONTEXT.md) describes dispatch and routing.
