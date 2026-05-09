You are @refactor-coder for Phase 8.6 in meridian-cli worktree /home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec.

You are not alone in the codebase. Other agents may edit disjoint tests in parallel. Do not revert or overwrite edits you did not make. Do not delete untracked files; leave .review-tmp/ untouched.

User goal: additional source refactors for architecture simplification, fewer concepts, fewer duplicated paths. No backwards compatibility needed.

Implement ONLY safe source simplifications from the explorer report. Scope/ownership:
- src/meridian/lib/state/paths.py
- src/meridian/lib/ops/spawn/api.py
- optionally, if still low risk and localized, session-reference helper extraction in src/meridian/lib/ops/reference.py, reference_recovery.py, session_target.py

Required edits:
1. Delete the unused workspace loader surface in state/paths.py: remove load_workspace_config() and _try_load_workspace_config() if no consumers remain. Keep workspace parsing in config/workspace.py.
2. Inline/delete thin spawn-operation carrier in ops/spawn/api.py: remove SpawnOperationServices, resolve_spawn_operation_services(), and trivial accessors if feasible; inline in the two cancel/cancel_all callers. Preserve behavior.
3. If and only if it remains simple/low-risk, extract a shared helper for repeated latest/normalized harness-session-id logic across reference.py/reference_recovery.py/session_target.py. If this starts spreading, do not do it; report as next-target.

Do not attempt full spawn pipeline redesign, service_context removal, or full session_target resolver merge.

Run targeted formatting/lint for changed files if practical. Final report must list files changed, source refactors completed, skipped targets and why, and verification run.
