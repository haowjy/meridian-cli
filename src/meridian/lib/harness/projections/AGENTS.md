# harness/projections/

Content projection — maps `HarnessLaunchSpec` fields to CLI flags, environment
variables, and payload content for each harness and launch mode.

## Files

- `_guards.py` — `check_projection_drift()`: import-time drift guard for field accounting
- `projection_errors.py` — `HarnessCapabilityMismatch`: unsupported mode/flag combinations
- `permission_flags.py` — `resolve_permission_flags()`: maps approval/sandbox modes to
  harness-specific CLI flags (shared by all projectors)
- `project_claude.py` — Claude subprocess and streaming projection
- `project_codex_common.py` — Shared Codex utilities: approval/sandbox mode mapping
- `project_codex_subprocess.py` — Codex subprocess (`codex exec --json`) projection
- `project_codex_streaming.py` — Codex streaming (`codex app-server`) projection
- `project_opencode_subprocess.py` — OpenCode subprocess (`opencode run`) projection
- `project_opencode_streaming.py` — OpenCode streaming (`opencode serve`) projection

## Entry Points

Each `project_<harness>_<mode>.py` exports a top-level function:
- `project_<harness>_spec_to_cli_args(spec) → (list[str], dict[str, str])`

Called by the adapter's `resolve_launch_spec()` → projection pipeline. Do not call
these directly from outside the adapter layer.

`resolve_permission_flags(permission_resolver, harness_id)` from `permission_flags.py`
is called inside each projector — it is not a separate pipeline step.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — drift guard mechanism, field accounting
  rules, permission flag mapping, Codex streaming split, OpenCode env-based projection

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — where projection sits in the
  translation pipeline; `_PROJECTED_FIELDS` / `_DELEGATED_FIELDS` invariant description
