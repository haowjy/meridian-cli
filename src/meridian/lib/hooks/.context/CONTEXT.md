# Hooks

Runtime dispatch drops and reports invalid rows while preserving valid rows and
default overrides. Inspection reports all row errors. Notification defaults
are ordinary lowest-layer rows: a matching disabled row silences delivery.

External hooks inherit no registered payload variables from the caller; absent
fields must stay absent rather than carrying another hook's context.

KB `hooks.md` holds the reasons behind hook failure handling, event naming and
autosync.
