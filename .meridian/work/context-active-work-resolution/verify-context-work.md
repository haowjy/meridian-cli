Verify the active-work context resolution implementation from spawn p4995.

Expected behavior:
- `meridian context work` returns active work directory, not configured work root.
- Resolution order for active work: `MERIDIAN_ACTIVE_WORK_DIR` if set/non-empty, else persisted current work state, else no active work.
- `work start` / `work switch` persist current work state.
- spawns can inherit active work env from resolved active work.

Please inspect changed files from p4995 and run appropriate focused tests/lint. Fix only mechanical issues if needed; report substantive problems. Do not revert unrelated changes or delete untracked files.
