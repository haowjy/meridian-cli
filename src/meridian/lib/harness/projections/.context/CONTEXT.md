# harness/projections/ — Context

## Architecture

Each projector maps a harness-specific `LaunchSpec` to the executable form:
`(list[str] command_args, dict[str, str] env_overrides)`. Two projectors exist per
harness where subprocess and streaming differ materially:

```
project_claude.py          → ClaudeLaunchSpec → args + env
project_codex_subprocess.py → CodexLaunchSpec → args + env   (codex exec --json)
project_codex_streaming.py  → CodexLaunchSpec → args + env   (codex app-server)
project_opencode_subprocess.py → OpenCodeLaunchSpec → args + env  (opencode run)
project_opencode_streaming.py  → OpenCodeLaunchSpec → args + env  (opencode serve)
project_pi_rpc.py          → ResolvedLaunchSpec → args + env (pi rpc --mode rpc)
project_pi_native_tui.py   → ResolvedLaunchSpec → args + env (pi native TUI, primary only)
pi_extension_projection.py → (extension dist artifacts → per-launch materialization, no spec dependency)
```

`project_codex_common.py` is not a projector — it provides `map_codex_approval_policy`
and `map_codex_sandbox_mode` utilities shared by both Codex projectors.

`pi_extension_projection.py` is not a projector — it resolves and materializes
Meridian-owned Pi extension entrypoints from `pi_runtime/dist/extensions/` into
per-launch state directories. It does not consume a `LaunchSpec`. See the Pi
Extension Projection section below.

## Contracts

### Drift Guard (`_guards.py`)

Each projector declares two frozensets at module level:

- `_PROJECTED_FIELDS` — spec fields the projector handles directly (turned into flags/env)
- `_DELEGATED_FIELDS` — spec fields handled upstream (by the adapter or spec builder)

`check_projection_drift(spec_cls, projected, delegated)` runs at module import time.
It compares `projected | delegated` against `spec_cls.model_fields`. If any field is
missing from both sets, `ImportError` is raised. If any field in the sets no longer
exists on the spec, `ImportError` is raised.

**Effect:** Adding a field to a harness `LaunchSpec` without updating the corresponding
projector causes import failure. This is not a test — it fires in production on the
first spawn. Fix by updating `_PROJECTED_FIELDS` or `_DELEGATED_FIELDS` in the
projector.

### Permission Flags (`permission_flags.py`)

`resolve_permission_flags(permission_resolver, harness_id)` is the single entry point
for all permission/sandbox flag projection. It:

1. Maps `config.approval` to harness-specific base flags via `_permission_flags_for_harness`
2. Calls `permission_resolver.resolve_flags()` for broker-resolved extra flags
3. Strips Claude-specific `--allowedTools`/`--disallowedTools` pairs for non-Claude harnesses
4. Logs a warning if `--disallowedTools` is dropped for Codex (unsupported)

Approval mode → flag mapping:

| Mode | Claude | Codex |
|---|---|---|
| `yolo` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` |
| `auto` | `--permission-mode acceptEdits` | `--full-auto` |
| `confirm` | `--permission-mode default` | `--ask-for-approval untrusted` |
| `default` | (none) | (sandbox flag if non-default sandbox) |

OpenCode receives no permission flags from this function — OpenCode handles approval
through workspace env injection (see parent `.context/`).

### Pi Extension Projection (`pi_extension_projection.py`)

Resolves and materializes Meridian-owned Pi extension entrypoints for each launch:

- **Source**: `pi_runtime/dist/extensions/<name>/index.js` — TypeScript extensions built
  with `pnpm run build:extensions`
- **Target**: `~/.meridian/meridian-pi/agent/extensions/<uuid4>/<name>/index.js` —
  per-launch directory to prevent stale cached extensions
- **Atomic copy**: uses `tempfile.mkstemp` + `shutil.copy2` + `os.replace` to avoid
  partial writes
- **Override env vars**: `MERIDIAN_PI_EXTENSION_SOURCE_ROOT` and
  `MERIDIAN_PI_EXTENSION_TARGET_ROOT` for testing

Two functions provide entrypoints:
- `resolve_pi_lifecycle_extension_entrypoint()` — lifecycle extension only (primary mode)
- `resolve_pi_all_extension_entrypoints()` — both managed-bash and lifecycle (spawned mode)

Raises `PiExtensionProjectionError` if a required built artifact is missing — directs
the user to run `cd src/meridian/pi_runtime && npm run build:extensions`.

### `HarnessCapabilityMismatch`

Raised when the requested launch configuration cannot be expressed on this harness.
Examples:
- Unknown approval mode passed to `map_codex_approval_policy`
- Unknown sandbox mode passed to `map_codex_sandbox_mode`

This is a caller error — the adapter layer should have validated the mode before
reaching projection. If you catch this, something in the adapter pipeline is wrong.

### OpenCode Streaming: Split Across Multiple Field Sets

`project_opencode_streaming.py` projects spec fields into four distinct destinations:

- `_SERVE_COMMAND_FIELDS` → CLI flags for `opencode serve` command
- `_SESSION_PAYLOAD_FIELDS` → JSON body of the POST `/session` request
- `_MESSAGE_FIELDS` → POST `/message` body fields (e.g., `appended_system_prompt`)
- `_REFERENCE_FIELDS` → rendered reference blocks in the session payload

The `_PROJECTED_FIELDS` frozenset is the union of all four. The drift guard validates
the union, not each sub-group individually — the separation is for internal routing only.

### Claude: Parent-Allowed-Tools Flag

`project_claude.py` parses `CLAUDE_PARENT_ALLOWED_TOOLS_FLAG` entries out of
`extra_args` before passing remaining args as passthrough. This internal flag is how
a parent Claude process grants tool permissions to a spawned child. It is stripped
from `extra_args` and merged into `--allowedTools` on the command line — it must not
reach the harness as-is.

## Patterns

### Adding a Field to a Harness LaunchSpec

1. Add the field to the Pydantic spec class in `launch_types.py`
2. In each projection module that covers this spec:
   - Add to `_PROJECTED_FIELDS` if this projector emits the field as a flag/env
   - Add to `_DELEGATED_FIELDS` if the field is consumed earlier in the pipeline
3. Re-run imports to confirm the drift guard passes (it fires at import, not test time)

### Adding a New Harness

Follow the full touchpoint list in [../.context/CONTEXT.md](../.context/CONTEXT.md).
For projections specifically: add `project_<harness>_subprocess.py` and/or
`project_<harness>_streaming.py`, declare `_PROJECTED_FIELDS` and `_DELEGATED_FIELDS`,
call `check_projection_drift()` at module level, and add any harness-specific approval
flag mapping to `permission_flags.py`.

## Related .context/

- [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — full translation pipeline;
  `SpawnParams` accounting invariant (the adapter-level counterpart to projection drift)
- [../../../../pi_runtime/.context/CONTEXT.md](../../../../pi_runtime/.context/CONTEXT.md) —
  source of built extension artifacts that `pi_extension_projection.py` materializes
