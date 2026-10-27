# meridian-cli — Context

## Design Tensions

**Policy vs mechanism**: CLI/ops are policy (what to do). Harness adapters are
mechanism (how to launch). The composition factory in `lib/launch/context.py`
is the seam. Don't put harness-specific logic in ops or CLI.

**Observation vs intrusion**: Meridian reads harness state. Where observation
requires machinery the harness doesn't provide, use the minimum and justify it
against a specific otherwise-unobservable value. Codex/OpenCode primaries inherit
the native terminal; Claude/Pi keep a PTY relay (`lib/launch/process/AGENTS.md`).

**Simplest orchestration**: No task graphs, retry trees, or agent state machines.
Launch, track, report. Orchestration logic belongs in agent prompts, not the
coordination layer.

**Knowledge in data**: Agent capabilities live in YAML profiles, not procedural
code. State lives in JSONL events and JSON files, not in-memory objects.

**Per-spawn cost**: launch, finalize, wait, show, cancel, and continue cost must
not grow with history size; no scan or fold over all spawns/sessions on a
per-spawn path (#586). Bar, measured examples, and known violators: KB
`architecture/per-operation-cost.md`.
