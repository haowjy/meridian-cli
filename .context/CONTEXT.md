# meridian-cli — Context

## Design Tensions

Full rationale with citations: KB `principles/design-principles.md`.

**Policy vs mechanism**: CLI/ops are policy (what to do). Harness adapters are
mechanism (how to launch). The composition factory in `lib/launch/context.py`
is the seam. Don't put harness-specific logic in ops or CLI.

**Observation vs intrusion**: Meridian reads harness state. Where observation
requires machinery the harness doesn't provide (e.g., PTY capture for Claude
session-ID extraction from a TUI), use minimum machinery and justify the
intrusion against a specific unobservable-otherwise constraint.

**Simplest orchestration**: No task graphs, retry trees, or agent state machines.
Launch, track, report. Orchestration logic belongs in agent prompts, not the
coordination layer.

**Knowledge in data**: Agent capabilities live in YAML profiles, not procedural
code. State lives in JSONL events and JSON files, not in-memory objects.
