You are @smoke-tester for Phase 8.6 verification in meridian-cli worktree /home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec.

Verify realer behavior boundaries affected by source simplification and test collapse. Do not edit files. Do not touch .review-tmp/.

Focus:
- CLI spawn dry-run/sanity/error smoke still passes after deleting CLI forwarding tests.
- Spawn ops/list/cancel/continue behavior remains covered by integration boundaries.
- Mars passthrough compact tests still exercise current behavior.

Suggested commands (use uv run pytest-llm as project wrapper):
- uv run pytest-llm tests/smoke/test_sanity.py tests/smoke/test_spawn_dry_run.py tests/smoke/test_spawn_errors.py
- uv run pytest-llm tests/integration/ops/test_spawn_continue.py tests/integration/ops/test_spawn_api.py tests/integration/ops/test_spawn_read_reconcile.py
- uv run pytest-llm tests/integration/cli/test_cli_mars.py tests/integration/cli/test_cli_spawn.py

Final report exact commands/results and any behavior concerns.
