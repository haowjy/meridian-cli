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

**Spawn-owned harness journals:** permission and control-action writes must use the
published-spawn artifact mutation seam at the write point. An awaited send or dispatch
can cross spawn deletion; a late journal or cursor write must not recreate the directory.
Pi runtime metadata uses that same publication gate.

Interactive Claude primaries receive a starting prompt through
[meridian-first-turn](../../../claude_runtime/meridian-first-turn/README.md)
when the mod can run. If the mod is found but fails to load, the prompt is
lost: there is no argv fallback after binding. Its plugin-origin transcript row
is injected activity, not a user return.
