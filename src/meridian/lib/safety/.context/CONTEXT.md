# Permission policy

A scoped `native` permission spelling can name a different tool from the one
it governs: Claude uses `Edit(path)` for scoped writes. Use `native_tools` to
decide which tools remain callable, not the spelling of `native`.

A scoped allow only pre-approves the calls it matches; it restricts nothing. A capability
with only scoped allows leaves unmatched calls to the other rules and `approval`. To restrict
it, the profile writes an unscoped `deny` beside the allows (`bash: deny`), or `'*': deny` for
a whole-policy allowlist.

Launch snapshots keep the profile's Mars rules; the policy is rebuilt at bind. On a
continue or fork, `_with_current_bundle_rules` (`launch/policies.py`) replaces the snapshot's
`project`-origin rules, and drops the snapshot rules Mars now lists as set aside, so a
project-wide deny added after the session started holds on replay; it can only restrict.
If Mars cannot be asked the replay fails. Completion
signals are always granted, while read and spawn commands remain subject to
narrower profile restrictions. See `docs/agent-profiles.md` for harness limits.
