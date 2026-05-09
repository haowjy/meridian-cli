You are @explorer for Phase 8.6 in meridian-cli worktree /home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec.

Goal: identify tests to collapse/delete and retained behavior coverage. Read-only. Do not edit files.

User priorities:
- Collapse/delete mocked unit/integration tests, fewer implementation-coupled tests.
- Unit tests presumptively deletable unless pure logic/hard-to-smoke risk.
- Mock-heavy integration tests presumptively deletable if behavior covered at realer boundary.

Inspect these files and nearby coverage:
A. tests/integration/cli/test_cli_spawn.py
B. tests/integration/cli/test_cli_main.py
C. tests/integration/cli/test_cli_mars.py
D. tests/unit/harness/test_codex_ws.py
E. tests/unit/launch/test_compiler.py
F. tests/unit/hooks/builtin/test_git_autosync.py
G. tests/unit/cli/test_primary_launch.py

Deliverable:
- For each target: delete entirely / trim to named tests / keep, with rationale.
- Identify existing smoke/integration/real-boundary coverage that remains after deletion.
- Recommend a small targeted verification command list for this pass.
- Flag any tests whose deletion would leave a high-risk behavior unprotected.
