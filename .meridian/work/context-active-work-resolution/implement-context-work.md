Implement the active-work resolution rework.

User intent:
- `meridian context work` should mean "where should current work artifacts go?" i.e. the active work item directory, not the work container/root.
- Resolution order for active work should be:
  1. `MERIDIAN_ACTIVE_WORK_DIR` if set and non-empty;
  2. persisted current work item state, conceptually `meridian work current`, if it resolves to something;
  3. no active work item.
- `meridian work switch` / `meridian work start` cannot set the parent shell env directly, so they should persist current work state in Meridian state. Later `meridian context work` should see that persisted state.
- The configured work container/root should remain accessible through admin/debug surfaces (e.g. existing `meridian work root`) but should not be what `meridian context work` returns.
- Spawns should receive `MERIDIAN_ACTIVE_WORK_DIR` from the resolved active work, so subagents write artifacts to the active work item.

Requirements file: .meridian/work/context-active-work-resolution/requirements.md

Likely relevant areas from prior read-only exploration:
- `src/meridian/cli/misc_commands.py` (`meridian context NAME` routing)
- `src/meridian/lib/ops/context.py`
- `src/meridian/lib/context/resolver.py`
- `src/meridian/lib/state/work_store.py`
- `src/meridian/lib/launch/prompt.py`
- tests around context/work commands

Implementation expectations:
1. Add/adjust persisted current-work lookup if needed.
2. Make `meridian context work` return active work item path with env-first/current-state fallback.
3. Make bare `meridian context` present active work consistently with the same resolution.
4. Ensure spawned agents get `MERIDIAN_ACTIVE_WORK_DIR` from resolved active work.
5. Add focused tests. Prefer existing test style. Use `uv run pytest-llm` only for focused tests if available/appropriate.
6. Do not delete untracked files or revert other changes.
7. Update docs only if needed for changed user-facing command semantics.
8. Report changed files and verification run.
