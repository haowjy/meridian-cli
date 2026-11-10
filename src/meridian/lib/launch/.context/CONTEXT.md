# Launch contracts and rationale

## Composition and identity

Preparation resolves policy and composes content; binding supplies per-run values
and may run for preview. Materialization writes only for real launches. Overlapping
finalizers cover partial setup and pre-run failure, relying on idempotent completion.

Bind replaces the inherited caller boundary with the new process's sandbox,
spawn capability and network policy in the child environment; the child-spawn
guardrail uses it to reject weaker child policy. User-config detection contributes warnings,
not the child guardrail or permission report. Projected roots are not part of that
guardrail. Mars owns model and harness selection and policy precedence; its route
trace is diagnostic, not a retry queue. The network default is resolved after final
sandbox precedence, retaining denies; old durable snapshots may use scalar policy.

Identity is assigned before exec and validated after teardown, before attribution.
An initial owned observation conflicting with the assignment or fork source fails the
attempt; later IDs are diagnostic. Only a verified exit chat replaces the entry chat
for spawn references and resume hints.

## Exact continue

Primary and spawn continue retain the spawn ID and replay the source task directory,
work attachment, session identity and policy snapshot, including an absent work
attachment. Ambient caller work or task state must not fill that absence. The
launch lock covers ending the previous launch, checking the next launch, reopening
and execution. Dry-run does not write a new launch.

Mars revalidates the recorded model and pinned harness against current restrictions.
An explicit model override is allowed with a cache/cost warning; agent, skills,
execution policy, passthrough, environment, work and task-dir changes are rejected.
The snapshot supplies loaded skills and the available-skills listing; it is not
re-resolved against the filesystem. Selection records mean accepted selection, not
model execution; a recording failure must not replay the turn. An unsupported named
model resume fails before startup instead of silently using the harness default.

The system prompt is recomposed from the snapshot. Identical inputs within one
Meridian version yield identical bytes for prompt caching; a changed goal or surface
can change them. Foreground and background spawns share a preamble. Claude's
`--system-prompt-snapshot` defaults on: Claude records the first request's system
prompt and reuses it on resume until compaction, even if Meridian passes different
text. Profile or launch-config edits therefore cannot reach that conversation until
compaction.

## Prompt and skills

Put `--from` prior output and reference files in the user turn, ahead of the current
request. Prior output can contain user-generated text and must not acquire the
system prompt's implicit authority. Keep spawn-specific IDs, launching chat and
session links there too: changing those bytes in the system prompt would invalidate
sibling prompt caches. A primary's own chat and links remain in its capability block
because continue replays no user turn. A primary goal is guidance, not a user turn:
otherwise a bare launch would submit it as the first message. A work item's goal is
looked up with `meridian work show` so the prompt cannot retain stale text or an ID.

Mars supplies skill bodies through the launch bundle. Do not filter loaded skills by
`model-invocable`: the harness-native skill list may omit them, and composition is
their delivery channel. Loaded skill headers say they are already loaded so agents
do not look for them in that list. Bundle `tools.mcp` contains permission names,
not MCP server configuration. Named skill delivery points at the base skill file,
including when Mars selected a harness variant; inline delivery includes the selected
body. The snapshot preserves the delivery mode on continue.

The Meridian skills block is a trigger index, not loaded content. It omits a skill
already loaded with nonblank content so the trigger cannot point to a different copy.
The block appears in both primary and subagent modes; each listed skill must have
no mode or match the profile's declared mode (default `subagent`). The agent catalog is also looked
up live through `meridian spawn subagents`, so a long session can see newly installed
agents without embedding the catalog in every prompt.

## Directories and references

A session's work and task paths are literal symlinks that follow work switches and
moves. The task path resolves from an execution pin, then its work item, then the
project root. Pin only a checkout the store cannot recompute: explicit task-dir, an
outside-project launch, or an inherited pin read from the launcher's spawn scope.
Do not inherit `MERIDIAN_TASK_DIR` itself: in a managed run it names the launcher's
link. A checkout derived from an item follows later work switches. Restricted shells
need literal paths because they cannot expand variables or run compound `cd` calls.

A missing work-item task directory falls through with a warning without changing
stored work state; callers must carry that warning into the request. `kb:` references
cannot escape the authority KB root.

Workspace projection grants access to relevant roots, including an external task
cwd, runtime, temporary and control socket directories. Claude and Codex use
`--add-dir`; OpenCode adds external-directory grants to its config. When projection
is inactive, an external task cwd yields a warning but the launch continues; the
harness may need separate access configuration. Do not use passthrough arguments as
a workspace projection channel.

## Environment and lifetime

`MERIDIAN_SECRET_*` values are stripped from every harness child, primaries included, so
in-session notification can't read them; ordinary inherited values can reach
transcripts. Each launch sets its own `_MERIDIAN_HARNESS` instead of inheriting an
ancestor's, so `spawn wait` uses the caller's yield interval, not its child's.


Ignoring direnv reverses its recorded diff before deriving overlays and propagates
the decision to descendants. The reversal can run a bounded subprocess during bind;
never expose its captured environment or stderr in errors or artifacts. It does not
stop tools from reloading `.env`.

The child PATH may be pinned to a shim for the launching Meridian build so hooks,
extensions and agents invoke the same build. The shim must not be placed in a
child-writable root: an agent could replace a command that later runs unsandboxed.
Bind plans the pin; materialization writes it. A dead interpreter fails rather than
falling back to another `meridian` on PATH and silently changing builds.

A session holds its entry chat lifetime lock until exit, even if `/clear` changes
its chat. Session teardown stops the session and reclaims session-owned scopes even
if stopping fails. Reclamation skips busy launch locks, then rechecks the row and
chat under the lock; a runner passes its own lock handle because acquiring the same
lock again would skip its own scopes. Do not add separate cleanup after the scope.

Use structlog in this layer: the spawn preparation diagnostic capture wraps stdlib
logging, while structlog follows a different output path.

## Execution

The startup watchdog bounds backend boot, connection and session handshake for
both streaming spawn surfaces. A harness turn executes once; a terminal error,
transport failure, timeout, guardrail or identity failure does not trigger launch-level
replay. Adapter readiness polling may retry an operation that is demonstrably
idempotent. Streaming budget checks consume normalized cumulative usage immediately
after event folding, before subscribers can backlog.

[Primary process contracts](../process/.context/CONTEXT.md) cover terminal ownership
and signals. [Harness context](../../harness/.context/CONTEXT.md) covers adapter
projection and prompt delivery.
