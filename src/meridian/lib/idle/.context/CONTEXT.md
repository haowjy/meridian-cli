# lib/idle/ — Context

## Rationale

Idle policy has one decision point because four harnesses each ship their own
adapter (TypeScript mods for Claude and Pi, launcher tasks for Codex and
OpenCode). If the guard list, the at-most-once stage rule and config precedence
lived in the adapters, four copies would drift, and the hard cases (late timers,
reloads, work still running when a turn ends) would be solved four times. So adapters report facts and run the timers core places, and this package
decides; `lib/harness` never imports it, which an AST layering test enforces.
The launcher-hosted sidecar exists because Codex and OpenCode primaries already
have a Meridian process holding the backend connection for the whole session,
so no plugin is needed there. Full record, including rejected alternatives:
KB `decisions/idle-notifications.md` (D-idle-core-decides,
D-idle-leaf-packages, D-idle-adapter-hosting, D-idle-push-in-flight,
D-idle-compaction-removed).

## Follow-ups

[FUTURE](FUTURE) holds structural cleanups along those same boundaries: making
the `external_events` sensor capability explicit instead of a `getattr` flag,
separating Codex's identity pin from a real arm, lifting the pure transition
closures out of `service.py`, giving the sidecar's error reporter one owner, and
surfacing harness-event drift when arms never see a user return.

## Background-task facts and wake

Adapters report the work a turn left running as `BackgroundTask` facts, in the host's own
terms mapped to a kind. `wake.py` alone reads them, including Meridian's own CLI grammar;
never classify spawn commands in an adapter. A running agent is certain, and so is a shell
whose last command is a foreground spawn creation or `spawn wait`; loops, detached spawns,
management commands, and commands followed by more work are maybe. An explicit wait
`--timeout` (minutes) replaces the yield deadline. A certain wake holds the push, and the
response stays owed (`push_owed_since_ms`) until a push is delivered or the user returns. A
known wake before cache expiry drops the warning.

Per harness: the Claude mod forwards native background-task facts; Pi forwards tracked
background managed-bash rows as shell facts when the full command fits its bound, `other`
otherwise. The OpenCode 2.x sensor forwards the background shells and subagents its
primary's own tools started, shell facts bounded the same way, and `agent`. Report only
work that wakes the session when it ends. It stops reporting a subagent waiting on a
permission prompt, and re-arms with another `turn_end` when facts change while the primary
is idle (same stretch, new anchor; a stage already sent stays sent). Answering that prompt
is a `user_prompt` first, so the stretch closes and the re-arm opens a new one. Codex and
OpenCode 1.x send none (#669).

## Sidecar and reopen

The sidecar is hosted in the primary TUI process; its liveness follows the TUI launch task,
not the backend observer stream. A primary reopen through Meridian closes any open stretch
in the launcher before exec: not in exit hooks (a killed terminal runs none), not in
adapters (engines cannot tell reload from resume, and reload recovery must keep working).
TTL detection reads the chat's launch-recorded native store via `MERIDIAN_CHAT_ID`; never
pass or derive a cwd, since agents can `cd` while the transcript stays filed under the
launch directory.
