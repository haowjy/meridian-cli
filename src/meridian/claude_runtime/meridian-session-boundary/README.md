# Claude session boundaries

This classic command-hook plugin reports native session identity to Meridian.
It is independent of idle notifications and loads for primary and spawned Claude
launches. Outside a prepared Meridian launch, its hooks do nothing.

`SessionStart` with source `startup`, `resume`, or `fork` records the initial
native key. Later start/end events replace the current key, while retaining the
initial one. A first-seen clear/compact event cannot stand in for missing startup.
In particular, `/clear` and in-process `/resume` end the old session and start another; neither switch is a
process exit. Only a final `SessionEnd` with reason `prompt_input_exit`, `logout`,
or `other` can supply an exit key.

The launcher provides a fresh nonce, a spawn-owned receipt path, and its Python
interpreter through `_MERIDIAN_CLAUDE_SESSION_BOUNDARY_{NONCE,PATH,PYTHON}`. The hook
command uses `exec` so the recorder's parent PID is the launched Claude process.
The Python recorder uses only the standard library, writes atomically with mode
0600, never creates a missing spawn directory, and emits no conversation output.
A nested process with the same nonce cannot replace another PID's receipt.

The adapter checks the bounded receipt against the launch nonce and child PID.
The native-run finalizer compares the initial key with the planned entry, then
validates the exact final transcript and its native header before creating or
reusing an exit chat. The entry chat is never rebound. No directory scan or timestamp can
substitute for these checks.

Missing hooks, hook timeouts, stale/corrupt receipts, missing transcripts and hard
kills may remain unverified. Existing historical runs are not retroactively
verified. Receipt files are `claude-session-boundary.json` under the spawn's
artifact directory; they are observations, not an alternate transcript source.

## Verification

`claude plugin validate <plugin-dir>` checks the manifest. `claude plugin test`
only supports native TypeScript modules, not this classic command-hook plugin.
The Python integration suite exercises the actual recorder subprocess and native
exit allocation. Runtime qualification must use real Claude in an isolated
HOME/config/runtime; record the known child PID, `/clear`, `/resume` and native
fork (`SessionStart.source=fork`) events, and exact final transcript header. Never infer lifecycle behavior from fixtures.
