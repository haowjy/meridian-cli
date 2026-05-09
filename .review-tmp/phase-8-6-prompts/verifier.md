You are @verifier for Phase 8.6 in meridian-cli worktree /home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec.

Verify the current working tree after source simplification and test collapse. Do not make substantive source/test changes; only fix mechanical lint/import breakage if required and clearly report it. Do not touch .review-tmp/.

Required commands:
1. uv run ruff check .
2. uv run pyright
3. Targeted remaining tests for edited/dependent domains. Prefer pytest-llm wrapper in this repo:
   - edited test files:
     tests/integration/cli/test_cli_spawn.py tests/integration/cli/test_cli_main.py tests/integration/cli/test_cli_mars.py tests/unit/harness/test_codex_ws.py tests/unit/launch/test_compiler.py tests/unit/hooks/builtin/test_git_autosync.py tests/unit/cli/test_primary_launch.py
   - source/dependent ops/realer boundaries:
     tests/integration/ops/test_spawn_continue.py tests/integration/ops/test_spawn_api.py tests/integration/ops/test_spawn_read_reconcile.py tests/integration/hooks/test_git_autosync_repo.py tests/integration/launch/test_launch_resolution.py tests/integration/launch/test_launch_process.py
   - smoke boundaries:
     tests/smoke/test_sanity.py tests/smoke/test_spawn_dry_run.py tests/smoke/test_spawn_errors.py

If a command is too long/slow, split it and report exact completed commands. Final report: commands/results, any fixes, any remaining failures/blockers.
