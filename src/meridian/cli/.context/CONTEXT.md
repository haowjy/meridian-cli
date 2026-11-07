# CLI context

## Startup and parsing

Read-only invocations avoid preparing runtime state. In-process callers must
provide an invocation scope; the root dispatcher does not fabricate options.

Cyclopts requires a value for optional string parameters, so bare `--fork` and
`--from` become the synthetic value `__SELF__` before parsing. Argument splitting
must skip that value. Mars arguments remain verbatim. The CLI's `--fork` identity
lock is not an ops invariant: programmatic callers may change identity on a fork.

Help hides generated negation aliases that do not change the default in the
formatter, preserving those forms in the parser for compatibility.

Mars passthrough rejects package mutations in a bare directory unless `--user`
is given, before injecting a root. This prevents Mars auto-init from writing a
project marker there; plain `mars init` remains project authoring.

## Session browse

Interactive browse is refused inside a managed session because the supervisor
cannot safely transfer the process. Start-from eligibility is independent of
liveness: `--from` reads recorded state, so a running session can be a source.
The launch uses the values shown in the picker; its resolver checks validity.
It always supplies the agent, avoiding the configured default for an agentless
source, and uses `--opt=value` so prompt text beginning with `-` stays a value.

Browse preloads cyclic imports before worker lanes run: concurrent first imports
can see partially initialized packages. Nothing may write to stderr while the
full-screen application runs.

## Spawn output and messages

New spawn subcommands default to JSON unless they surface content agents need
to read directly. The hidden `spawn collect` and `spawn ack` output shapes are
a protocol with the Claude runtime mod in
`src/meridian/claude_runtime/meridian-spawn-watch/hooks/register.ts`; change
its parser and these output shapes together.

`spawn message` frames the message as text with the sender and, when available,
a reply command: recipients otherwise cannot tell who sent it or how to reply.
The reply uses the sender's current chat so it survives `/clear`; its control
socket must exist for a reply route to be offered. A Claude primary binds that
socket only after Claude publishes its inbox; bare or pre-2.1.224 primaries have
no reply route. A socket file can remain after its server dies, so its existence
is not a connection check.

The frame must remain message text so every harness receives it. Claude's
peer-message envelope carries `from` and `from-mode`, which Meridian does not
forge. A chat reference targets its live occupant: native `/resume` can move a
process into a chat started by another run. See
[session reference resolution](../../lib/ops/.context/CONTEXT.md).

[Bootstrap context](../../lib/bootstrap/.context/CONTEXT.md) describes state
preparation after classification.
