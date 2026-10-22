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
project_cursor.py          → ResolvedLaunchSpec → args        (cursor agent)
project_pi_rpc.py          → ResolvedLaunchSpec → args + env (pi --mode rpc)
project_pi_native_tui.py   → ResolvedLaunchSpec → args + env (pi native TUI, primary only)
pi_extension_projection.py → (built extension artifacts → stable -e paths, no spec dependency)
```

`project_codex_common.py` is not a projector — it provides `map_codex_approval_policy`
and `map_codex_sandbox_mode` utilities shared by both Codex projectors.

`pi_extension_projection.py` is not a spec projector — it resolves stable
Meridian-owned Pi extension entrypoints without copying or materializing them.
It does not consume a `LaunchSpec`. See the Pi Extension Projection section below.

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

### Cursor Projection (`project_cursor.py`)

#### Model + effort resolution

Cursor projection now treats `spec.model` as already-resolved runnable ID and passes it
through directly to `--model`. It does not perform local slug/effort matching.
Mars launch-bundle resolves Cursor effort into `routing.harness_model`, and Meridian
bind selects that value as `effective_model` before `ResolvedLaunchSpec` is created.
So by projection time there is nothing to resolve locally.

#### `_PROJECTED_FIELDS` excludes `"effort"`

Cursor still receives `effort` on `ResolvedLaunchSpec`, but projection no longer
consumes it. The field is delegated/accounted to keep drift-guard coverage complete.

#### MVP capability restrictions

The following raise `HarnessCapabilityMismatch` immediately:
- `spec.mcp_tools` set — per-spawn MCP tools not supported
- `spec.continue_fork` set — fork-based session continuation not supported
- `spec.continue_session_id` non-empty — session resume not supported
- `spec.interactive` set — interactive mode not supported

These are architecture gaps, not user errors. The adapter layer should filter them
before reaching projection; if they arrive here, something upstream is wrong.

#### `projected_roots` is silently ignored

Cursor subprocess only projects `--workspace` from `task_cwd`. If `projected_roots`
is non-empty, the projector logs a debug warning and continues — not an error, because
`task_cwd` is the common case and extra roots are best-effort for cursor.

### Permission compile (`permissions_<harness>.py`)

`spec.permission_policy` (`lib/safety/policy.py`) is transport-neutral: tool rules plus the
resolved sandbox and approval modes. Each harness module compiles it to a typed value and
its projectors spell that value:

| Harness | Compile result | Spelled by |
|---|---|---|
| Claude | tool availability, plain denies, scoped permissions, sandbox and prompt mode | `project_claude.py` (`--tools`, `--disallowedTools`, one `--settings`, permission flags) |
| Codex | sandbox mode, approval policy, shell/web/image switches | `project_codex_subprocess.py` (`--sandbox`, `-c`/feature flags), `project_codex_streaming.py` (process flags and the thread payload) |
| OpenCode | ordered config-agent rules for spawns and primaries | `OpenCodeAdapter.project_launch_env()` → version-native `OPENCODE_CONFIG_CONTENT` |
| Cursor | `--yolo`/`--force` choice, `--sandbox` value | `project_cursor.py` |
| Pi | `--tools`/`--exclude-tools`, plus rules for the `meridian-permissions` extension's `tool_call` block | `project_pi_rpc.py` / `project_pi_native_tui.py` (argv); `PiAdapter` env `_MERIDIAN_PI_PERMISSION_POLICY` (extension rules) |

Claude emits `ask` only as `settings.permissions.ask`; it never contributes to an allow
channel. Claude checks deny, then ask, then allow, so a plain deny or ask beside scoped
allows of the same tool would shadow them. The compile therefore emits no such plain rule
and gates the capability through the permission mode instead, like a per-capability
allowlist: a mode in which unmatched calls need approval, which a headless spawn refuses.
`approval: auto|never|yolo` gates with `acceptEdits` (edits keep running; it also runs file
commands such as `touch`, `rm` and redirects inside the working directories), unless an
edit capability is gated, which needs `manual`; `default` and `confirm` gate with `manual`.
`dontAsk` is reserved for a whole-policy allowlist (`*: deny`), which also limits
`--tools`; it refuses every tool not allowed, so it would turn a bash allowlist into a
whole-policy one. Ask rules hold under every mode, bypass included, so they do not change
the mode. Passthrough `--allowedTools` grants are merged and dropped where a profile
allowlist or gate owns the tool. Passthrough `--dangerously-skip-permissions`,
`--allow-dangerously-skip-permissions` and `--permission-mode` stay (the compile omits its
own mode when passthrough sets one) and the report says they override the profile; the
user, project and local settings files' allows, which Claude merges itself, are read by
preflight (`CLAUDE_SETTINGS_ALLOWS_FLAG`) and reported when an allowlist or gate is present.
Every Claude report line is read back from the emitted channels, so a rule the deny/ask
order shadows or the compile left out is `partial`.

When Claude's sandbox is enabled, the settings JSON always spells
`autoAllowBashIfSandboxed`: true only for an unrestricted `approval: auto`, false for
allowlists, gates and `ask`. Omitting false lets Claude's sandbox auto-approval accept
unmatched Bash calls before the mode can refuse them, which defeats the allowlist.

Codex compiles exact unscoped `bash`, `web`/`web_search` (the narrower wins), and `image` allow/deny rules to process-level
switches. Its scoped rules, `ask`, and allowlists have no native carrier and remain
unsupported. `project_codex_tool_switch_flags` is the one spelling shared by `codex exec`
and `codex app-server`; the thread payload continues to carry sandbox and approval only.

A compile that restricts the shell must name the `completion-commands` platform rule's
`commands` (`meridian spawn done` / `rearm`) in its own scope syntax, where a broad `bash` or
`*` deny does not shadow them; otherwise the child cannot answer the completion confirmation.
Every spawn carries the rule. OpenCode adds `bash(<cmd>)` and `bash(<cmd> *)` after its
own `*` default, so the rule never makes a map on its own. Claude adds `Bash(<cmd>)` and
`Bash(<cmd> *)` to `settings.permissions.allow` on every spawn (Claude's default mode asks
before commands that change files, and a spawn has no one to answer) and, for a whole
allowlist, keeps Bash available through `--tools`; a plain `bash` deny or ask is gated
rather than emitted so it cannot shadow them, and a scoped profile deny or ask that covers
them still wins and makes the platform line partial. Pi's permissions extension grants
only the whole command with optional `[A-Za-z0-9_.-]+` arguments, and loads for them only when some rule could restrict the shell; Codex cannot scope
its shell and reports the rule `unsupported`.

Each compile also returns an `EnforcementReport` (`lib/safety/policy.py`): one `ReportLine`
(`element`, `status`, `note`) per tool rule, platform rule, sandbox and approval value, saying
what that harness did with it. Status `native` means enforced as stated, `partial` means part
of the meaning reaches the harness (the note says which), `unsupported` means nothing does,
`platform` means a Meridian platform rule decided it. The report states today's
projection, not the intended one: write the note from what the compile emitted. The compile
is registered on `HarnessProjectionPorts.permissions`; `LaunchContext.enforcement_report()`
calls it for the `--dry-run` "Permissions:" section (and `permission_report` in `--json`)
and for `permission_report` in `projection-manifest.json`.

### Adding a New Harness

Follow the full touchpoint list in [../../.context/CONTEXT.md](../../.context/CONTEXT.md).
For projections specifically: add `project_<harness>_subprocess.py` and/or
`project_<harness>_streaming.py`, declare `_PROJECTED_FIELDS` and `_DELEGATED_FIELDS`,
call `check_projection_drift()` at module level, and put any harness-specific permission
compile in `permissions_<harness>.py`.

## Related .context/

- [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — full translation pipeline;
  `SpawnParams` accounting invariant (the adapter-level counterpart to projection drift)
- [../../../../pi_runtime/.context/CONTEXT.md](../../../../pi_runtime/.context/CONTEXT.md) —
  source of stable built extension artifacts that `pi_extension_projection.py` resolves
