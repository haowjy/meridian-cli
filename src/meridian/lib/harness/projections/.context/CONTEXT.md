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

`project_codex_common.py` is not a projector — it holds the Codex spellings both Codex
projectors share (sandbox, writable-root and network settings, tool switches, and
built-in-agent flags). The shared sandbox value spells `--add-dir` for subprocesses and
Codex config for app-server startup/thread creation so those boundaries cannot drift.
`native_config.py` flattens
the selected harness-native config, spells Codex TOML literals, and owns the reserved keys
that Meridian's Codex projections write themselves. Meridian writes no Codex MCP config,
so `mcp_servers.*` passes through. `compaction.py` owns the semantic
`autocompact` / `autocompact_pct` projection and report: Claude gets its token flag and
percentage environment variable, Codex gets config overrides, and the other harnesses
report both fields unsupported.

One warning-release exception is tracked in [TODO](TODO): a native
`sandbox_workspace_write.network_access` leaf is deprecated. The Codex permission compile
resolves it once and carries it on `CodexPermissions`; while present, the portable value is
omitted so the native value wins on subprocess, app-server and thread creation. The next
release makes it reserved like `writable_roots` and removes that one compile-time carveout.

`PermissionPolicy.network_projection=false` marks a bundle from Mars < 0.18. Compiles emit
no portable network setting in that state: Claude and Codex retain their constrained
sandbox defaults, while OpenCode, Pi, and Cursor remain unrestricted. The report's single
network line is `network: harness default`.

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

Mars names the candidate `agent` and `task` platform denies in
`tools.capability_natives`. Bind selects them (`agent` unless native agent copies are on;
`task` on spawns unless the profile has an unscoped `task: allow|ask`) and appends them to
the ordinary rule sequence with `origin="platform"`; compiles must not keep a second
native-name table. A platform deny overrides the profile's rules for its capability rather
than taking part in specificity: Claude turns those rules into denies (else its plain deny
beside a scoped allow would compile as a gated allowlist), and OpenCode emits the deny after
every profile rule in place of the profile's entries for that action. `builtin-agents` and
`meridian-commands` remain separate platform mechanisms because they are not ordinary tool
denies.

| Harness | Compile result | Spelled by |
|---|---|---|
| Claude | tool availability, plain denies, scoped permissions, sandbox and prompt mode | `project_claude.py` (`--tools`, `--disallowedTools`, one `--settings`, permission flags) |
| Codex | sandbox mode, approval policy, shell/web/image switches | `project_codex_subprocess.py` (`--sandbox`, `-c`/feature flags), `project_codex_streaming.py` (process flags and the thread payload) |
| OpenCode | ordered config-agent rules for spawns and primaries | `OpenCodeAdapter.project_launch_env()` → version-native `OPENCODE_CONFIG_CONTENT` |
| Cursor | `--yolo`/`--force` choice, `--sandbox` value | `project_cursor.py` |
| Pi | `--tools`/`--exclude-tools`, plus rules for the `meridian-permissions` extension's `tool_call` block | `project_pi_rpc.py` / `project_pi_native_tui.py` (argv); `PiAdapter` env `_MERIDIAN_PI_PERMISSION_POLICY` (extension rules) |

Claude emits `ask` only as `settings.permissions.ask`; it never contributes to an allow
channel. Claude checks deny, then ask, then allow, so a plain deny or ask beside scoped
allows of the same tool would shadow them. "The same tool" is either a tool the scoped
allow concerns (its `native_tools`) or the permission it is spelled with: Claude checks
paths only through `Edit(path)`/`Read(path)`, so a scoped write allow is `Edit(src/*)`, and a
plain `Write` (the write allowlist's deny) and a plain `Edit` (`edit: deny`) both shadow it.
The compile therefore emits no such plain rule (a per-capability allowlist arrives as one,
written by the policy) and gates the capability through the permission mode instead: a
mode in which unmatched calls need approval, which a headless spawn refuses. `--tools`
under a whole allowlist is `PermissionPolicy.allowed_tools` plus Bash for the Meridian command
grants: the tools, not the permission spellings, so `write(src/*)` keeps `Write` callable.
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
the Claude bind into `ClaudeRuntimeResolution.settings_allows` and reported when an
allowlist or gate is present.
Every Claude report line is read back from the emitted channels, so a rule the deny/ask
order shadows or the compile left out is `partial`.

When Claude's sandbox is enabled, the settings JSON always spells
`autoAllowBashIfSandboxed`: true only for an unrestricted `approval: auto`, false for
allowlists, gates and `ask`. Omitting false lets Claude's sandbox auto-approval accept
unmatched Bash calls before the mode can refuse them, which defeats the allowlist. A launch
with spawn capability also spells `excludedCommands = ["meridian spawn *"]`, including when
the profile leaves the sandbox at `default` and the user's own settings enable it; the
default projection omits `enabled` so the user's choice remains authoritative. Claude runs a
matching standalone command wholly outside the Bash sandbox. Compound commands stay inside
and the spawn operation refuses them when its current `/proc/self/ns/pid` differs from the
namespace the Meridian launcher recorded. On systems without readable procfs the check skips.

Codex compiles exact unscoped `bash`, `web`/`web_search` (the narrower wins), and `image` allow/deny rules to process-level
switches. Its scoped rules, `ask`, and per-capability allowlists have no native carrier and
remain unsupported; a per-capability allowlist's deny (origin `allowlist`) sets no switch,
so the shell stays on for Codex to read files with. A whole-policy allowlist disables web
search and image viewing but deliberately leaves that shell on. Platform-origin rules never
drive these tool switches; the built-in-agent mechanism still drives Codex's multi-agent
feature switch. `project_codex_tool_switch_flags` is the one spelling shared by `codex exec`
and `codex app-server`. Native config also goes to both paths as process-level `-c`
overrides; the app-server thread payload remains authoritative for its model, effort,
sandbox, and approval fields, so those native collisions are skipped and reported.

OpenCode decides a headless `approval: default` spawn's unmatched calls by a toggle carried
from v0.11.8: without a `*` rule, the compile emits `*: allow` only when no profile rule
names an OpenCode tool; one such rule leaves OpenCode's own defaults, and the user's OpenCode config, in charge.
The toggle is deliberate: Meridian stays out of a user's OpenCode setup whenever the profile
says something. The compile reports whichever branch applied as an
`approval: default` line, which follows the policy elements since `default` approval is
not one.

A compile that restricts the shell must spell every `meridian-commands` entry in its own
scope syntax, where a broad `bash` or `*` deny does not shadow it. Completion and read
entries are present on every launch; profiles with spawn capability also receive the
`meridian spawn` prefix. Each entry declares a completion/read/launch role and exact or
prefix matching. Completion grants are final; read and launch grants let a narrower or
equal scoped profile deny/ask remain stricter. OpenCode maps exact
entries to `shell(<cmd>)` and prefix entries to `shell(<cmd> *)` after its own `*` default.
Claude maps an exact entry to `Bash(<cmd>)`, and a prefix to both `Bash(<cmd>)` and
`Bash(<cmd> *)`; under a whole allowlist it also keeps Bash available through `--tools`.
A plain `bash` deny or ask is gated rather than emitted so it cannot shadow the commands.
Claude cannot make a completion allow override a scoped deny/ask and reports that gap as
partial. OpenCode orders completion grants last. Pi short-circuits a matching completion
grant and otherwise evaluates profile specificity. Codex cannot scope its shell per launch.
Under `read-only` or
`workspace-write`, its Meridian-owned user rules file grants the whole `meridian spawn`
prefix outside the sandbox for every session using that Codex home; the compile reports the
remaining exact/prefix list as unsupported. A rules-file write failure is reported without
blocking the launch.

Each compile also returns an `EnforcementReport` (`lib/safety/policy.py`): one `ReportLine`
(`element`, `status`, `note`) per tool rule, platform rule, sandbox, network and approval
value, saying
what that harness did with it. Status `native` means enforced as stated, `partial` means part
of the meaning reaches the harness (the note says which), `unsupported` means nothing does,
`platform` means a Meridian platform rule decided it. The report states today's
projection, not the intended one: write the note from what the compile emitted. The
compile's report is registered as `HarnessProjectionPorts.permission_report` (the projectors
call the typed compile directly for its native value; nothing generic reads that value);
`LaunchContext.enforcement_report()` calls it for the `--dry-run` "Permissions:" section (and
`permission_report` in `--json`) and for `permission_report` in `projection-manifest.json`.
Each harness also registers `HarnessProjectionPorts.effective_network`: a pure function of
`EffectiveNetworkFacts` (sandbox, network, projection availability, and native config).
Its `network:` report line and the child-spawn guardrail call that same owner directly, so
the public three-field `ReportLine` has no hidden propagation state.

Every compile reports the effective shell `network` policy separately from built-in web
tools. Claude projects allowed domains plus local-binding/Unix-socket settings when its
Bash sandbox is enabled; on Linux host localhost remains partial because clients must use
the sandbox proxy. Claude's own settings files can merge allowed domains back into a deny,
and the report names those sources. Codex enables network only for `workspace-write`;
`read-only` reports an explicit authored allow as unsupported while an unset/defaulted
allow is informational. OpenCode, Pi, and Cursor satisfy allow through their unrestricted
shells and report deny as unenforced. A `meridian spawn` sandbox escape remains visible on
the network line because that command runs with host network access.

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
