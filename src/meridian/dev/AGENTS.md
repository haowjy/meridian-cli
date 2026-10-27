# dev/ — Contributor Tooling

Developer-only utilities, not part of the CLI surface: `pytests.py` (`pytest-llm`
entry point) and `preflight.py` (concurrent fast-gate runner behind
`scripts/preflight.sh`).

Run tests with `uv run pytest-llm`, not `uv run pytest`: the wrapper trims output
(`--tb=line`, `--maxfail=1`). `PYTESTS_LAST_FAILED=1` adds `--lf --lfnf=all`.
