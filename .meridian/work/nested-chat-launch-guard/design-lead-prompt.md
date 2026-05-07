User wants to remove the nested `meridian chat` launch guard because it prevents delegated smoke testers from running real chat E2E. Current symptom: inside spawns with `MERIDIAN_DEPTH=2`, `uv run meridian chat --headless ...` fails with “meridian chat requires a root Meridian process.” This caused recent chat shared-policy work to pass only fallback seams rather than live root chat smoke.

Work item: nested-chat-launch-guard
Source of truth: .meridian/work/nested-chat-launch-guard/requirements.md

Please design the correct architecture for removing/replacing the nested chat launch guard. Explore the current chat launch/session lifecycle enough to identify why the guard exists, what risks it covers, and what targeted protections should replace it. Produce design artifacts under `.meridian/work/nested-chat-launch-guard/design/`.

Focus:
- Let nested spawns launch live chat for smoke testing.
- Preserve isolation: no session bleed, no shared port collision, no unsafe lifecycle ownership.
- Keep chat on shared launch/policy resolution for model/harness/agent/skills/approval.
- Define behavioral spec and verification strategy, including live delegated smoke.
- Call out whether this is trivial guard removal or requires lifecycle changes.

Do not implement. Return whether design is ready for planning or if runtime probes are needed.
