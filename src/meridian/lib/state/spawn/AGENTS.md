# Spawn state

Decide mutations against the locked snapshot. Quarantined rows are not empty
results: they may contain active work, so callers must handle quarantine before
destructive actions.

[.context/CONTEXT.md](.context/CONTEXT.md) covers finalization and legacy reads.
