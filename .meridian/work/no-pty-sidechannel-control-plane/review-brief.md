Review the no-PTY sidechannel control-plane direction before implementation planning.

Source of truth: `requirements.md`.

Focus:
- Is the product framing sound: remove PTY from Codex/OpenCode rendering while keeping Claude PTY?
- Are hooks/injection/permission requirements internally consistent?
- What risks would make a no-PTY side-channel design unsafe?
- What acceptance criteria or probes are missing before implementation?
- Watch especially for default-deny permission regressions, Codex interrupted-turn stale output, OpenCode blocking `/message`, replay/refire hook bugs, and Windows terminal behavior.

Read-only. Do not edit files. Return findings with severities and recommendations.
