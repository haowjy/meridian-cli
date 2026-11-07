# Harness context

## Native identity and completion

Pi UUID collision preflight skips unreadable sibling headers with a warning: a torn
journal must not block the shared store for an improbable UUID4 collision. A matching
readable ID or failed store enumeration refuses the launch. This is not an atomic
reservation; resume and fork read the exact source journal and matching header,
and post-execution checks verify the assigned identity. Missing or mismatched
evidence refuses the operation or fails identity validation.

Post-exit observation verifies the planned identity before publishing an exit. Claude
uses a launch-nonce and child-PID correlated hook receipt and validates the exact native
transcript; `clear` and `resume` switch the current session. Claude has no
verified final-exit receipt: its last observed session is not proof of the exit identity. Trampoline successor
evidence is diagnostic only. Missing receipts do not trigger transcript scans.

Harness event names are not globally unique. A connection's primary scope distinguishes
parent events from child events: child events can reach subscribers, but cannot
complete the parent turn or change its activity. Codex accepts an unscoped `turn/completed` as parent;
OpenCode does so only before a parent scope is known, because its global stream also
carries child events.

A connection delivers specific terminal evidence before EOF; a later generic close must
not hide it. Cursor's successful process exit is its success boundary. Pi's `agent_end`
is an attempt outcome, not completion: settlement, native idle and quiescence must all
hold. Pi remains alive for child completion and notifications, so process exit cannot
signal completion. Resident Codex and OpenCode also wait for persisted descendants.
See [Pi drain](../../streaming/.context/pi-drain.md), [Pi integration](pi-integration.md)
and [transport contracts](../connections/.context/CONTEXT.md).

## Message delivery

`acknowledged` means a harness answered the message request; `posted` means the write
completed without a receipt. Neither proves the model saw or executed it. A timeout after
a write leaves delivery uncertain and must not cause a replay.

Claude's stdin write has no acceptance receipt. Codex may fall back from a
rejected steer RPC to start, but not after timeout or transport failure. OpenCode's
message receipt does not prove a turn was scheduled; its V1 path has not been run
against a 1.x server. Cursor has no injection transport.

Managed Codex and OpenCode primary routes remain attached through idle silence, but
refuse sends after detecting the TUI has moved to another session. They stay scoped to
the first session. Some OpenCode switches are invisible until the human types; see
[the integration guide](../../../../../docs/harness-integration.md). Claude's route
waits for the exact child's inbox; Pi's TUI extension owns its own route.

## Claude

Claude's TUI exposes its primary session ID on stdout only when stdout is a TTY, so
Meridian captures it through a PTY. The Windows fallback is untested.

Built-in agents are denied by default; `allow_builtin_agents` removes that restriction.
Generic `Agent` follows the Mars native-agent copy ledger. Claude cannot deny generic
`Agent` while retaining built-ins, so the platform agent deny is reported partial in
that combination; `SendMessage` and `ListAgents` remain denied. These tools can reach
agents and sessions outside Meridian.

Claude's system prompt uses a file because large skills and profiles can exceed
`ARG_MAX`. The file must exist before the child starts.

## Codex

In managed-primary attach, Meridian is the first app-server client and owns approval
requests; the TUI observes them. Spawn requests are auto-rejected; primary requests are
journaled and surfaced without an in-tree reply. The control socket carries messages,
not approval answers. [Live-fork snapshots](codex-live-fork.md) describe fork publication.

## OpenCode prompt, agent and model delivery

OpenCode's prompt, permissions, model and workspace grants share the bound
`OPENCODE_CONFIG_CONTENT`. Existing unrelated settings and agents survive; Meridian
replaces its reserved agent entry and appends workspace grants. OpenCode merges
same-named agents across config documents, so an independently defined reserved ID can
still participate in that merge.

The prompt is a file reference in the reserved primary agent. Select that agent whenever
opening or resuming a session: a resumed session can retain its prior agent, and
`default_agent` alone does not override it. The V2 bare TUI cannot select an agent on
resume. Replacing the base prompt preserves AGENTS.md, the skill listing and the
environment block on both versions. The agent carries no model; V2 ignores that field.

The prompt file must exist before OpenCode starts: a missing reference fails config
loading. OpenCode expands `{file:}` before parsing JSON, ends the path at the first `}`,
and treats quotes, backslashes and controls literally; such paths are refused. Keep
UTF-8 unescaped because a JSON Unicode escape becomes literal path text. Substitution is
single-pass, so references inside the prompt remain text.

A fresh session receives the selected model; continue preserves its native selection
unless an explicit model switch is requested. V2 bare TUI needs `--standalone` so the
launch environment takes effect; V1 rejects that flag. V2 bare TUI rejects `--model`,
so it receives the fresh model via config. There is no working V2 effort channel.

OpenCode V2 migrates V1 databases in place and retains V1 tables. A V1 fallback after
using V2 needs a separate data root. Storage readers choose V2 when `session_v2` exists,
not from the installed binary. See [session transcripts](session-transcripts.md).

A stalled event stream is not necessarily dead: a long-running tool may emit no events.
The connection checks whether the session still runs and keeps reading when it does;
spawned drains have a bounded busy stall, while primary observers reconnect after the
turn ends to resync idle state.

## OpenCode background work

Idle pushes wait only for work that will wake the primary. Live observations establish:

| Work | Wakes primary? |
|---|---|
| Background agent shell or subagent tool | Yes |
| Session shell HTTP request or standalone shell HTTP request | No |
| Child session created directly through HTTP | No |

Track only shell or child work named by a primary tool event; creation alone is not
proof it will wake the session. Continuing an existing child has no creation event and
may be missed. A child awaiting permission is excluded so the user can see the prompt;
a successful answer closes the idle stretch and re-arms it. Only permission waits are
tracked. Interrupted foreground work can end after the terminal event, so deletion and
failure events must also end tracking.

The event stream is live-only, so a headless start subscribes before it posts the first
prompt: a quick turn that ended before the drain attached would lose its text and, once
`events()` returned, its hold. On reconnect, the idle sensor reconciles the primary's
activity and known work; starts and complete turns lost in the gap cannot be recovered.
A failed work reconciliation drops the hold rather than waiting forever. The shell
listing is location-scoped: use the session location, not the ignored `directory` query.
A fresh server loads providers on first location access; touch the location before a
headless prompt to avoid a provider routing race.

## Projection and further reading

Permission behavior and harness-specific limits are in [projection contracts](../projections/.context/CONTEXT.md).
For adding a harness, use the [integration guide](../../../../../docs/harness-integration.md).
