You are @coder for Phase 8.6 test collapse in meridian-cli worktree /home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec.

You are not alone in the codebase. Other agents may edit source files in parallel. Do not revert or overwrite edits you did not make. Do not delete untracked files; leave .review-tmp/ untouched.

User goal: collapse/delete mock-heavy unit/integration tests; keep only small current-behavior coverage where needed. Net-negative LOC welcome. No backwards compatibility needed.

Ownership: edit only these test files unless a tiny conftest/import cleanup is necessary:
- tests/integration/cli/test_cli_spawn.py
- tests/integration/cli/test_cli_main.py
- tests/integration/cli/test_cli_mars.py
- tests/unit/harness/test_codex_ws.py
- tests/unit/launch/test_compiler.py
- tests/unit/hooks/builtin/test_git_autosync.py
- tests/unit/cli/test_primary_launch.py

Use explorer recommendations:
A. test_cli_spawn.py: trim to CLI-only glue: stdin prompt-file through main, prompt+prompt-file conflict, runtime-error no traceback, background agent-mode wire/no event noise, explicit json rich wire/events, children text rendering. Delete continue/fork/list/cancel forwarding matrix and helper microtests not needed.
B. test_cli_main.py: keep only top-level bridge tests with no cleaner replacement: harness shortcut route, init --link mars init/link branch selection, bootstrap documents flag. Delete dispatch/help/telemetry/chat-management duplicates.
C. test_cli_mars.py: collapse into small parametrized coverage for passthrough streaming/sync augment, MERIDIAN_MANAGED only for sync, mars list agent-mode text defaults.
D. test_codex_ws.py: keep app-server command projection, thread request projection (collapse if easy), fail-closed permission mapping; optionally one runtime policy guard. Delete lifecycle/request routing private internals.
E. test_compiler.py: keep policy/precedence/provenance behavior; delete dataclass/source-shape/immutability/serialization tests.
F. test_git_autosync.py: keep only pure helper tests for exclusion/path parsing; delete mocked execute/rebase/lock matrix.
G. test_primary_launch.py: keep only resume/fork user-facing failure wording tests; delete wiring duplicates.

After edits, run targeted pytest for the remaining edited files if practical (use uv run pytest ...). Final report: files changed, tests deleted/collapsed, behavior coverage retained, verification run, and any risk.
