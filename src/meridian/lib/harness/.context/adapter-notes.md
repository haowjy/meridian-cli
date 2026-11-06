# Adapter notes

Claude's spec bind port reads the settings files selected by the final child cwd and
environment. Tool allows from user and project files live on
`ClaudeRuntimeResolution.settings_allows`. The adapter separately warns when the selected
user settings contain a non-empty `sandbox.network.allowedDomains` or either proxy-port
key. That report-only detection never changes the projected network or child guardrail;
narrowing settings are not modelled. Project network settings did not widen a launch-local
strict allowlist in the live probe, so they are not detection inputs.
The adapter's `shell_network_guidance()` is the sole owner of Claude's Linux-only
host-service proxy hint. `project_claude_network()` returns both its native settings and
typed effective policy; compile reads the former, while the report and child-spawn
guardrail read the latter. Shared prompt composition only places the returned guidance.

Codex's adapter warns about any `allow` under `features.network_proxy.domains` in both the
child-selected `$CODEX_HOME/config.toml` and `.codex/config.toml` in the child cwd or any
ancestor. A boolean `features.network_proxy` declares no domains. Detection is report-only:
it does not model proxy activation, project trust, or narrowing `deny` entries and never
changes the projected network or guardrail. Meridian still emits only its own inline map.
Both harness readers distinguish a missing file from an unreadable one; unreadable files
produce attributed warnings.

- **Codex constrained-launch execpolicy:** real-launch materialization atomically maintains only
  `$CODEX_HOME/rules/meridian.rules` for spawn-capable `read-only` and `workspace-write`
  launches. Its
  `meridian spawn` allow applies to every Codex session using that home. A write failure is
  reported but does not block launch.

**Spawn-owned harness journals:** permission and control-action writes must use the
published-spawn artifact mutation seam at the write point. An awaited send or dispatch
can cross spawn deletion; a late journal or cursor write must not recreate the directory.
Pi runtime metadata uses that same publication gate.

An interactive Claude primary with a starting prompt (fresh, resumed or forked) receives it through
[`meridian-first-turn`](../../../claude_runtime/meridian-first-turn/README.md), not argv,
unless the mod will not run (`bind_claude_spec` decides once: mod not found, `--bare` /
`--safe-mode` or their env switches, settings with `disableAllHooks` / `allowManagedHooksOnly` / `allowManagedModsOnly`, Claude older than 2.1.314; those keep the positional prompt).
Its transcript row is `type: user` with `origin.kind: "plugin"`; Meridian's transcript
readers match on role and text, and `meridian-idle` counts a plugin origin as injected, not a
user return. If the mod is found but fails to load, the prompt is lost (not verified
that Claude reports an error): no fallback re-adds it to argv once Meridian has dropped it. A refused or failed
submit puts the prompt in the composer with a toast; a delivered launch writes
`first-turn-delivered` beside `starting-prompt.md`, which gates resubmission after a reload.
`_MERIDIAN_CLAUDE_FIRST_TURN_PATH` is in `BLOCKED_CHILD_ENV_VARS`, so a launch never inherits it.
