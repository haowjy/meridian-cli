Implement the user's requested follow-up: allow nested/delegated agents to run `meridian chat` management subcommands too.

Context:
- Commit `fdad9656` allowed nested `meridian chat` launch by removing the root-process guard from startup only.
- Current remaining guard blocks `chat ls/show/log/close` in nested Meridian executions.
- User asked whether it matters and stated they think these should be allowed.

Scope:
- Remove/narrow the nested root-process guard for chat management commands so `chat ls`, `chat show`, `chat log`, and `chat close` are allowed from nested spawns.
- Preserve normal argument validation and shared runtime behavior.
- Do not bypass policy resolution for launch paths.
- Update docs/smoke guides that still say chat management is root-only.
- Update integration tests that currently assert nested management fails.
- Add/adjust tests proving nested management no longer fails solely because of `MERIDIAN_DEPTH`.

Verification:
- Focused integration tests around chat CLI management and startup.
- Live CLI smoke with `MERIDIAN_DEPTH=2` for management commands enough to prove no root-process guard error. Use safe commands first; for `close`, avoid killing unrelated user servers if possible by using a temp/disposable server or document why only parser/path-level smoke was safe.
- ruff/pyright on touched files if practical.

Constraints:
- Shared repo: never revert/stash/reset/delete unknown files.
- Commit only files you changed with a descriptive message when tests pass.
- Return changed files, tests run, commit hash, and any caveats.
