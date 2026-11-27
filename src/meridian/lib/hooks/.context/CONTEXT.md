# Hooks

Runtime dispatch drops and reports invalid rows while preserving valid rows and
default overrides. Inspection reports all row errors. Notification defaults
are ordinary lowest-layer rows: a matching disabled row silences delivery.

External hooks inherit no registered payload variables from the caller; absent
fields must stay absent rather than carrying another hook's context.

A machine event (`EventSpec.machine`; today `inbox.received`) has no project. Its rows
are valid only in the user layer, so a row elsewhere is an invalid row like any other,
and `config.load_machine_hooks_config_report` reads that layer without opening a project
file: a repository must not be able to replace or stop a row the user put in their own
`~/.meridian/config.toml`. Its context has
no `project_root`, the runner then uses the home directory, and it takes no `interval`
(no runtime root to keep the state). `machine.launch_inbox_received` starts
`machine_event` detached, with stdio on `/dev/null` and `env_registry.session_free_env`:
a program that runs `meridian message inbox` and reads its output to end of file would
otherwise wait until every row finished, since the rows would hold the pipe open, and one
that does so while holding a lock would keep the lock that long. That process's stderr goes nowhere, so it records what each row did
in the inbox's hook log (`lib/inbox`), then prunes.

KB `hooks.md` holds the reasons behind hook failure handling, event naming and
the context sync daemon (`lib/autosync`).
