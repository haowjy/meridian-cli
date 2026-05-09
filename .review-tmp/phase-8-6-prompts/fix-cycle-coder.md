You are @coder for Phase 8.6 review fix cycle in meridian-cli worktree /home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec.

You are not alone in the codebase. Do not revert broad edits. Do not use git reset/stash/clean/revert. Do not delete untracked files; leave .review-tmp/ untouched.

Review gate requested changes after test collapse. Implement a minimal fix that preserves the collapse but restores high-value pure contract coverage and finishes a small source ownership cleanup.

Tasks:
1. tests/unit/launch/test_compiler.py
   - Restore a minimal subset of pure contract tests removed by the collapse, not the old full suite.
   - Cover these behaviors if existing fixtures make it practical:
     a) per-field precedence independence
     b) legacy-model fallback behavior
     c) timeout exclusion from overlay/profile defaults
     d) child CLI override beats parent overlay
   - Keep concise/table-driven where possible. Do not restore dataclass/source-shape/serialization/immutability tests.

2. tests/unit/cli/test_primary_launch.py
   - Keep the file small, but restore 2-3 lean wrapper contract tests for run_primary_launch:
     a) cross-harness rejection for continue/fork
     b) resume request shaping threads source execution cwd / claude config / chat/session metadata into SessionRequest
     c) fork request shaping if easy; otherwise one request-shaping test is acceptable.
   - Do not restore the full old suite.

3. Source cleanup for config/workspace ownership:
   - Inspect src/meridian/lib/config/workspace.py and src/meridian/lib/state/paths.py.
   - If config/workspace still imports private _load_workspace_table or _merge_nested_dicts from state.paths, move those helpers into config/workspace (or a small config-owned helper) and remove the state.paths copies if no longer used.
   - Goal: workspace config parsing no longer lives in state.paths and pyright no longer reports an unused private state helper from this change.

Run targeted verification:
- uv run ruff check src/meridian/lib/config/workspace.py src/meridian/lib/state/paths.py tests/unit/launch/test_compiler.py tests/unit/cli/test_primary_launch.py
- uv run pytest-llm tests/unit/launch/test_compiler.py tests/unit/cli/test_primary_launch.py
- uv run pyright (or at minimum pyright on touched source/tests if full is slow; report exactly)

Final report: files changed, restored tests count/behaviors, source cleanup, verification, risks.
