# Requirements — nested chat launch guard

## Problem statement

`meridian chat` currently refuses to launch from nested Meridian execution (`MERIDIAN_DEPTH>0`). That blocks delegated smoke testers and implementation agents from running live chat E2E. As a result, chat policy/model/agent/skill behavior can only be verified through fallback seams, hiding exactly the class of bugs this work is meant to catch.

## User intent

Remove the nested chat launch guard so spawned agents can run real `meridian chat` smoke tests. The goal is not to weaken safety generally; it is to make chat launch safe and testable from delegated execution.

## Desired outcomes

- Live `meridian chat` startup works from nested Meridian spawns.
- Delegated smoke testers can run root-equivalent chat E2E without faking environment depth.
- Any real safety concerns previously covered by the depth guard are replaced with targeted protections.
- Chat session ownership, state writes, port allocation, backend lifecycle, and logs remain isolated enough to avoid session bleed.
- Management commands remain safe and predictable.

## Non-goals / constraints

- Do not bypass shared policy resolution.
- Do not reintroduce chat-specific model/agent/skill resolution.
- Do not fake root execution by mutating `MERIDIAN_DEPTH` in tests.
- Preserve crash-only/file-authority design.
- Prefer smoke-testable behavior over seam-only verification.
