# harness/projections/ — Spec-to-Args Projection

Maps `HarnessLaunchSpec` fields to CLI flags, environment variables, and payload
content for each harness and launch mode. Step 2 of the translation pipeline
(`HarnessLaunchSpec` → `list[str] + env dict`).

## Mental Model

Each projector is a pure function: `spec → (argv_additions, env_overrides)`. It
knows everything about how a specific harness and mode receives its configuration —
which flags exist, which are mutually exclusive, what goes in env vs argv.

**Drift guards run at import time.** Each projector declares `_PROJECTED_FIELDS`
(fields it maps to argv/env) and `_DELEGATED_FIELDS` (fields owned by the caller).
`check_projection_drift()` runs when the module loads — if any spec field is missing
from both sets, it raises `ImportError`. Adding a field to a `HarnessLaunchSpec`
without updating the corresponding projector → startup failure.

## Per-Harness Notes

- **Claude** (`project_claude.py`): single module covers both subprocess and streaming;
  uses `--append-system-prompt-file` channel to avoid `ARG_MAX` limits.
- **Codex** (`project_codex_subprocess.py` + `project_codex_streaming.py`): split because
  `codex exec` and `codex app-server` accept different flag sets.
- **OpenCode** (`project_opencode_subprocess.py` + `project_opencode_streaming.py`):
  workspace projection goes through `OPENCODE_CONFIG_CONTENT` env var (JSON), not flags.
- **Permission flags** (`permission_flags.py`): shared across all projectors.
  `resolve_permission_flags()` is called inside each projector — not a separate pipeline step.

## Key Rules

**Don't call projectors directly from outside the adapter layer.** They are called
by `adapter.resolve_launch_spec()` — that's the only intended callsite.

**Adding a field to a `HarnessLaunchSpec` requires updating `_PROJECTED_FIELDS` or
`_DELEGATED_FIELDS` in the corresponding projector.** Missing either → `ImportError`
at startup.

## Entry Points

Each `project_<harness>_<mode>.py` exports one top-level function:
`project_<harness>_spec_to_cli_args(spec) → (list[str], dict[str, str])`.

`_guards.py` — `check_projection_drift()`: the import-time enforcement mechanism.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — drift guard mechanics, field accounting
   rules, permission flag mapping table, OpenCode env-based projection detail.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — translation pipeline; where
   projection fits; `_PROJECTED_FIELDS` / `_DELEGATED_FIELDS` invariant.
