# dev/ — Contributor Tooling

Developer-only utilities, not part of the CLI surface: `pytests.py` (`pytest-llm`
entry point) and `preflight.py` (concurrent fast-gate runner behind
`scripts/preflight.sh`).

`uv run pytest-llm` runs the default paths with trimmed output; the full suite is still
`uv run pytest tests/`. The wrapper trims output
(`--tb=line`, `--maxfail=1`). `PYTESTS_LAST_FAILED=1` adds `--lf --lfnf=all`.
