# Review: nested chat launch guard design

## Task

Review the design package for removing the nested chat launch guard. Challenge the architecture choice and spec completeness. Focus on:

1. **Does the spec cover the actual risks?** The explorer found high-risk issues around discovery file collision and recovery cross-talk. Are these adequately addressed by the behavioral spec?

2. **Is Option B (isolated runtime root) the right call?** The architect recommends a per-chat-server scoped runtime root under `<project runtime root>/chat-servers/{root,spawn-<id>}/`. Challenge whether this is over-engineered vs under-engineered.

3. **Missing edge cases**: What happens when:
   - A nested chat server outlives its parent spawn?
   - Multiple nested spawns try to launch chat servers concurrently?
   - The `MERIDIAN_SPAWN_ID` is not set in a nested context?
   - Someone runs `meridian chat` (non-headless) from nested?
   - The project runtime root doesn't exist yet?

4. **Scope creep risk**: Is this trying to solve more than needed? The requirements say "let nested spawns launch live chat for smoke testing." Does the architecture add unnecessary generality?

5. **Alternative I want you to consider**: What if instead of a new directory layout, we just:
   - Remove the guard
   - Skip discovery file write when nested
   - Require `--url` for management commands when nested
   - Accept that recovery cross-talk is a theoretical risk that doesn't matter for short-lived headless smoke tests?

   Is this "good enough" simpler alternative actually sufficient?

## Source of truth

- Requirements: `.meridian/work/nested-chat-launch-guard/requirements.md`
- Behavioral spec: `.meridian/work/nested-chat-launch-guard/design/spec/behavioral-spec.md`
- Architecture: `.meridian/work/nested-chat-launch-guard/design/architecture/target-architecture.md`
- Explorer findings: `.meridian/work/nested-chat-launch-guard/design/exploration-findings.md`

## Output

Write review findings to `/home/jimyao/gitrepos/meridian-cli/.meridian/work/nested-chat-launch-guard/design/review-findings.md`. Include severity (blocking, major, minor) for each finding.
