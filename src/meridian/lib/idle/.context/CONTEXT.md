# lib/idle/ — Context

## Rationale

Idle policy has one decision point because four harnesses each ship their own
adapter (TypeScript mods for Claude and Pi, launcher tasks for Codex and
OpenCode). If the guard list, the at-most-once stage rule and config precedence
lived in the adapters, four copies would drift, and the hard cases (late timers,
reloads, a compaction's own turn, a user returning mid-compaction) would be
solved four times. So adapters report facts and execute `act`, and this package
decides; `lib/harness` never imports it, which an AST layering test enforces.
The launcher-hosted sidecar exists because Codex and OpenCode primaries already
have a Meridian process holding the backend connection for the whole session,
so no plugin is needed there. Full record, including rejected alternatives:
KB `decisions/idle-notifications.md` (D-idle-core-decides,
D-idle-leaf-packages, D-idle-adapter-hosting).

## Follow-ups

[FUTURE](FUTURE) holds structural cleanups along those same boundaries: making
the `external_events` sensor capability explicit instead of a `getattr` flag,
separating Codex's identity pin from a real arm, lifting the pure transition
closures out of `service.py`, giving the sidecar's error reporter one owner, and
surfacing harness-event drift when arms never see a user return.
