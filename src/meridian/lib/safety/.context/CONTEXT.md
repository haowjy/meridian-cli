# Permission policy

A scoped `native` permission spelling can name a different tool from the one
it governs: Claude uses `Edit(path)` for scoped writes. Use `native_tools` to
decide which tools remain callable, not the spelling of `native`.

A scoped allow only pre-approves the calls it matches; it restricts nothing. A capability
with only scoped allows leaves unmatched calls to the other rules and `approval`. To restrict
it, the profile writes an unscoped `deny` beside the allows (`bash: deny`), or `'*': deny` for
a whole-policy allowlist.

Launch snapshots keep Mars rules; the policy is rebuilt at bind. Completion
signals are always granted, while read and spawn commands remain subject to
narrower profile restrictions. See `docs/agent-profiles.md` for harness limits.
