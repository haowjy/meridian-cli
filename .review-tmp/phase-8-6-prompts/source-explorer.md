You are @explorer for Phase 8.6 in meridian-cli worktree /home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec.

Goal: identify safe, high-value source simplifications for this single pass. Read-only. Do not edit files.

User priorities:
- Large architecture refactor; simplify/delete concepts, fewer ways to implement same thing.
- No backwards compatibility needed. No real users/data.
- Focus on obvious collapse/deletion, avoid risky deep redesign if not finishable in one pass.

Targets to inspect:
1. Spawn orchestration duplication: core/spawn_service.py prepare/service seam vs ops/spawn/api.py and ops/spawn/execute.py old row creation / launch-context assembly paths. Find safe collapse/delete candidates.
2. Session reference resolution duplication: ops/reference.py, ops/reference_recovery.py, ops/session_target.py, primary/session log/repair handling. Find obvious duplicated helpers/result shapes to collapse.
3. Runtime/bootstrap carriers: ApplicationContext, RuntimeReadContext, RuntimeWriteContext, RuntimeAuthoritySnapshot, OperationRuntime. Determine if service_context.py removal is feasible in one pass or list pass-through/test-only entrypoints to delete.
4. Config/workspace helper split: workspace parsing helpers straddling config/state boundaries; suggest small ownership cleanup if safe.

Deliverable:
- Ranked list of concrete source edits with file paths and rationale.
- For each: risk level, expected tests affected, and why it reduces concepts/fan-out.
- Explicitly mark anything too risky for this pass as next-target.
