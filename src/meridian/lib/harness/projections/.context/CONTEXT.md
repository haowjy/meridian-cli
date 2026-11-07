# Harness projection contracts

A permission compile maps the transport-neutral policy to a native value and an
enforcement report. Platform agent and task denies override profile rules; their native
names come from Mars, not a second table. Completion grants for `meridian spawn done`
and `meridian spawn rearm` cannot be restricted by profile rules; read and launch
grants allow an equally or more specific profile restriction. Harness limitations
can make a grant partial or unsupported.

| Report status | Meaning |
|---|---|
| `native` | Enforced as stated |
| `partial` | Only part of the rule reaches the harness; the note says which part |
| `unsupported` | No part reaches the harness |
| `platform` | A Meridian platform rule decided it |

Report what was actually emitted. The effective policy used by the child-spawn guardrail
uses the same Claude and Codex network projection functions as native emission; user and project config may widen actual
access and is warned about separately. OpenCode and Pi have no effective-policy port and
are treated as unrestricted. Shell network policy is separate from built-in web tools.

## Claude

Claude evaluates deny, then ask, then allow. A plain deny or ask shadows scoped allows
for the same native tool or permission spelling, including `Write` or `Edit` beside a
scoped write allow expressed as `Edit(path)`. The compile gates unmatched calls through
permission mode instead. Headless spawns refuse calls requiring approval. `auto` cannot
serve as that gate because its classifier approves unmatched calls; `acceptEdits` can
also run file commands, so edit gates need `manual`. `dontAsk` is reserved for a whole
policy allowlist because it refuses every other tool. Ask rules hold even in bypass mode;
plain deny and ask also hold under `auto` on Claude Haiku 5.5.

Passthrough permission modes can override the profile and are reported. Claude merges
user, project and local settings; their allows are reported when a gate or allowlist is
present. `autoAllowBashIfSandboxed` must be explicitly false for gates, allowlists and
ask: otherwise sandbox auto-approval can bypass them.

A launch with spawn capability excludes `meridian spawn *` from Claude's Bash sandbox,
even when sandbox enablement comes from user settings. Claude runs matching standalone
commands outside the sandbox; compound commands stay inside, and the spawn operation
refuses a different PID namespace when procfs is readable.

Claude's finite network rules (an explicit allowlist, rather than unrestricted
network access) use domain allow and deny lists. Local binding and Unix
sockets are explicitly disabled unless localhost is allowed, so wider user settings do
not survive. Enabling either is partial because the switches are coarse; Linux host
localhost also requires the sandbox proxy. Spawn's sandbox escape has host network access
and remains visible in the report.

## Codex

Unscoped web and image rules use process switches. Shell rules (every `bash` deny or ask,
and scoped rules beside them) use a Meridian `PreToolUse` hook, not `shell_tool=false`, which
would also disable Meridian's own commands; a `bash` allow needs nothing. Scoped ask and
other scoped capabilities have no carrier.
The hook cannot ask, so unscoped ask becomes deny. Codex silently skips untrusted hooks
and fails open on hook failures: the launcher verifies the enabled, trusted hook and its
hash through `hooks/list` before starting the thread, and the hook converts execution
failures to denial. No shared Codex config file is written for the hook.

The hook matches command segments by wildcard, like Pi's permission extension. It is an
accident guard, not a sandbox: wrappers, substitutions, scripts, interpreters and
`apply_patch` or MCP calls can bypass it. `bash_fail_closed` adds a simple-command check;
do not add a shell parser to close individual gaps. Hook enforcement is reported partial.

Codex native config passes through as process overrides except keys Meridian owns, such
as model, sandbox and approval. The removed `sandbox_workspace_write.network_access`
key is rejected on fresh launches and ignored in replayed snapshots so their stored
network policy remains authoritative.

Codex network restrictions apply only under `workspace-write`; proxy-based restrictions
are partial because local services remain reachable. `read-only` stays offline. Unset
network denies access in constrained Claude and Codex sandboxes. Cursor, Pi and OpenCode
report restrictions unenforced. Under Codex's constrained sandboxes, a Meridian-owned
user rules file grants the `meridian spawn` prefix outside the sandbox for every session
using that Codex home; a write failure is reported without blocking launch.

## OpenCode

OpenCode applies the last matching rule. Order wildcard defaults, unscoped rules, then
scoped rules by specificity, with stricter equal matches last. Platform denies follow
profile rules; completion grants follow profile restrictions, and workspace grants come
last. With headless `approval: default` and no wildcard rule, Meridian emits `*: allow`
only when the profile names no OpenCode tool; otherwise native defaults and user config
remain in charge.

## Effort and cursor

Claude cannot turn thinking off on current models: even `MAX_THINKING_TOKENS=0` was
observed to think. Pi maps `none` to `off` and clamps effort to the model. OpenCode V1
uses variants; V2 has no working effort channel. Cursor receives a model already resolved
with effort by Mars. Cursor is unmaintained; keep it building without adding parity work.
Additional workspace roots are best-effort: Cursor receives only `task_cwd` as its
workspace.
