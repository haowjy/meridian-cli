Design a Meridian architecture for no-PTY interactive harnesses with side-channel observability/control.

Use the requirements in `requirements.md` as source of truth.

The user direction is:
- Codex and OpenCode likely should not need PTY for their interactive TUI rendering.
- Claude still needs PTY.
- Meridian must remain on the event/control path for hooks, external-event loops, and injected user messages.
- Injection has two modes: interrupt then inject; inject-only.
- Permissions/HITL must be preserved and must not regress into default-deny behavior.

Deliver a design package under this work item's `design/` directory. Include:
1. recommended architecture;
2. per-harness launch/control matrix;
3. event/control dataflow diagrams in text or Mermaid;
4. injection semantics by harness;
5. permission/HITL routing design;
6. risks and probes still needed;
7. implementation boundaries and migration order.

Do not edit source code. Do not implement. Produce design only.
