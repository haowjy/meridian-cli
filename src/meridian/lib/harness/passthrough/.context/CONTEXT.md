# Managed-primary attach contracts

Codex's port reservation closes the socket before the backend binds, leaving a race. The caller handles `PortBindError` by choosing a new port.

Codex `resume --remote` rejects permission overrides such as `--add-dir`; workspace roots go to the app-server configuration.

OpenCode V1's attach command cannot carry a starting prompt, so its connection posts the prompt before attaching. V2's bare TUI carries the prompt itself; skipping the V1 post loses the prompt.

OpenCode V2's server password travels in `ObserverEndpoint.client_env` to the TUI process, not to spawn artifacts. The client does not accept URL userinfo or a password flag.
