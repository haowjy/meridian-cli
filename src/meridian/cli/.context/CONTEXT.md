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

`meridian message` (and its deprecated alias `spawn message`) frames the message as
text with the sender and, when available, a reply command: recipients otherwise
cannot tell who sent it or how to reply. The reply begins `meridian message`, with
`-C` after the address only for a sender whose ID is not machine-wide: a harness
lets a command out of its sandbox by its first words. The reply uses the sender's
current chat so it survives `/clear`; its control
socket must exist for a reply route to be offered. A Claude primary binds that
socket only after Claude publishes its inbox; bare or pre-2.1.224 primaries have
no reply route. A socket file can remain after its server dies, so its existence
is not a connection check.

`send_message` returns a result and prints nothing; one printer prints it as text
or `--json`. Kinds are assigned by phase: before the first byte reaches the socket
a failure has the kind of its cause, and after it any outcome without a parsed
answer is `maybe_delivered`, so a script that resends on a plain failure never
resends a message that arrived. Startup errors print the same object under
`--json` through the descriptor's `startup_error` shape. An `@` address goes to
the program in `[message] people` of the user's `config.toml`, read from no other
file: the command runs outside the sandbox, and a repository file is content a
sandboxed session can write.

The frame must remain message text so every harness receives it. Claude's
peer-message envelope carries `from` and `from-mode`, which Meridian does not
forge. A chat reference targets its live occupant: native `/resume` can move a
process into a chat started by another run. See
[session reference resolution](../../lib/ops/.context/CONTEXT.md).

[Bootstrap context](../../lib/bootstrap/.context/CONTEXT.md) describes state
preparation after classification.
