# Permission policy

A scoped `native` permission spelling can name a different tool from the one
it governs: Claude uses `Edit(path)` for scoped writes. Use `native_tools` to
decide which tools remain callable, not the spelling of `native`.

The policy adds an unscoped deny for a capability with only scoped allows.
Otherwise a harness may interpret a broad overlapping allow as permitting every
call. Codex skips that synthetic deny for its shell switch, which cannot express
the scoped exceptions.

Launch snapshots keep Mars rules; the policy is rebuilt at bind. Completion
signals are always granted, while read and spawn commands remain subject to
narrower profile restrictions. See `docs/agent-profiles.md` for harness limits.
