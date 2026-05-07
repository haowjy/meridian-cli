Fix verifier finding from p5004.

Problem:
- Active-work rework added persisted current-work state.
- Verifier found `work_done_sync(...)` does not clear the persisted current-work pointer when the done item is current.
- Likely `work_update_sync(... status="done" ...)` has the same issue.

Task:
1. Update work lifecycle so marking a work item done clears persisted current work if that item is current.
2. Cover both `work done` and status update-to-done paths if both exist.
3. Add focused regression tests.
4. Run focused tests/lint.
5. Do not modify unrelated files or revert others' changes.

Relevant files:
- `src/meridian/lib/ops/work_lifecycle.py`
- `src/meridian/lib/state/current_work.py`
- tests under `tests/unit/ops` / related active-work tests
