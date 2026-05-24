# Implementation: Backend Extension System B0–B3

## Context

The backend extension system design and plan have been approved after 3 design passes, an adversarial review, a scalability review, and planning. You are driving implementation through all 4 phases.

## Plan Files

All plan files are in the work directory under `plan/`:
- `plan/overview.md` — overall plan with Lane A (frontend) and Lane B (backend) sequencing
- `plan/phase-0-app-server-locator.md` — B0: locator, --host, /api/health, token auth (R1, R8, R9, R10)
- `plan/phase-1-extension-core.md` — B1: types, registry, dispatcher, observability, first-party commands (R5, R7)
- `plan/phase-2-http-adapter.md` — B2: extension routes, discovery, invoke, auth, RFC 9457 (R6)
- `plan/phase-3-cli-mcp.md` — B3: CLI ext commands, MCP tools, parity tests
- `plan/status.md` — phase/subphase tracking

## Design References

Key design docs (all under `design/`):
- `architecture/backend-architecture-synthesis.md` — THE master reference (§1-§14)
- `architecture/extension-command-core.md` — ExtensionCommandSpec types, registry, dispatcher
- `architecture/app-server-locator.md` — PID-keyed dirs, locator, failure modes, token auth
- `architecture/surface-adapters.md` — HTTP/CLI/MCP adapter specs, error mapping
- `refactors.md` — R1-R13 with dependencies
- `decisions.md` — locked decisions

## Critical Implementation Details

These details were surfaced during adversarial review and MUST be followed:

1. **Lifespan write goes in `server.py`** (NOT `app_cmd.py`) — `app_cmd.py` only adds `--host` and passes params
2. **Token file uses `os.open(mode=0o600)`** — NOT `atomic_write_text` (R10)
3. **`create_app()` gets `project_uuid` with default `"test-project-uuid"`** for test compat (R9)
4. **`state_root` in design docs = `runtime_root` in codebase** — use `runtime_root` in code
5. **ALL handler sub-dispatch methods pass 3 args** `(args, context, services)` (R5)
6. **`lib/spawn/archive.py` must only import from `lib/state/`** — no app layer (R7 cycle constraint)
7. **Static routes before `/{extension_id}`** in FastAPI registration order (B2 path shadowing)
8. **`ext` must be added to `AGENT_ROOT_HELP`** in `app_tree.py` (B3)
9. **URL construction uses `spec.extension_id` and `spec.command_id`**, never `fqid.split(".")`

## Phase Ordering

Strict: B0 → B1 → B2 → B3

Each phase has an exit gate in its plan file. Run the gate checks before moving to the next phase:
- `uv run ruff check .`
- `uv run pytest-llm`
- `uv run --extra dev python -m pyright`
- Phase-specific smoke tests

## Commit Cadence

Commit after each subphase that passes checks. Don't accumulate changes across phases. Each commit should be independently reviewable.

## What NOT to Do

- Do NOT touch frontend code (Lane A is separate)
- Do NOT implement Phase 4 (runtime bridge) or Phase 5 (frontend palette)
- Do NOT implement R2, R3, R4 (deferred refactors) or R11, R12, R13 (streaming, pre-A4b track)
- Do NOT migrate existing HTTP routes — extension system is additive
- Do NOT modify `OperationSpec` or `OperationSurface`
