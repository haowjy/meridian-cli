# Permission policy

A scoped `native` permission spelling can name a different tool from the one
it governs: Claude uses `Edit(path)` for scoped writes. Use `native_tools` to
decide which tools remain callable, not the spelling of `native`.

One precedence on every harness: of the rules that match a call, deny, then ask, then
allow; `'*'` only when none matches. How narrow a key is never matters, so a scoped allow
only pre-approves calls no stricter rule matches. To confine a tool to some commands the
profile writes `'*': deny` with the allows (a whole-policy allowlist); `bash: deny` beside
`bash(rg *): allow` turns Bash off. Meridian's command grants are decided before any rule
on Pi, Codex and OpenCode; Claude checks its denies and asks first, so there a matching
deny or ask still restricts a grant.

A place that reads rules for something other than emitting them (a permission-mode gate, a
Codex switch, tool availability) takes the strictest answer itself: a rule Mars covered
keeps its place in the list, and a continued snapshot's rules never pass through Mars
again. `complete_report` reports a covered rule (no native names, a Mars note) with Mars's
note on every harness, whatever a compile said, and marks a shell deny or ask `partial` where
the harness decides Meridian's command grants first and one lies in its scope.

Launch snapshots keep the profile's Mars rules; the policy is rebuilt at bind. On a
continue or fork, `refresh_project_rules` (`launch/policies.py`) re-reads the project-origin
rules: every project-wide deny refreshes, and a project-wide allow or ask refreshes only when
the agent's `*` rule (and, under `*: deny`, its allows) still matches the snapshot's, so a
refresh never opens a tool the snapshot closed. If Mars cannot be asked the replay fails.
Only profile and overlay rules can opt a spawn out of the platform `task` deny. See
`docs/agent-profiles.md` for harness limits.
