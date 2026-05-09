You are @verifier for Phase 8.6 final re-gate after review fixes in /home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec.

Do not edit except mechanical lint/import fixes if absolutely required. Leave .review-tmp/ untouched.

Run and report:
- uv run ruff check .
- uv run pyright
- uv run pytest-llm tests/integration/cli/test_cli_spawn.py tests/integration/cli/test_cli_main.py tests/integration/cli/test_cli_mars.py tests/unit/harness/test_codex_ws.py tests/unit/launch/test_compiler.py tests/unit/hooks/builtin/test_git_autosync.py tests/unit/cli/test_primary_launch.py
- uv run pytest-llm tests/integration/ops/test_spawn_continue.py tests/integration/ops/test_spawn_api.py tests/integration/ops/test_spawn_read_reconcile.py tests/integration/hooks/test_git_autosync_repo.py tests/integration/launch/test_launch_resolution.py tests/integration/launch/test_launch_process.py
- uv run pytest-llm tests/smoke/test_sanity.py tests/smoke/test_spawn_dry_run.py tests/smoke/test_spawn_errors.py
