# Design review — nested chat launch guard

> [!NOTE] **Audit trail status** — These findings were produced before the latest spec and architecture revisions. The blocking and major issues called out below have been addressed in the current design package, but the original review text is retained unchanged as decision/audit history.

Overall assessment: **request changes**. The package is directionally correct in rejecting the “just remove the guard” shortcut — the explorer already showed recovery cross-talk is a concrete mutation risk, not a theoretical one. Option B is the right general direction. But the current spec and architecture still disagree on core behavior, and the proposed nested scope key does not actually give per-server isolation.

## Blocking findings

### 1. Discovery semantics are contradictory between the behavioral spec and the target architecture
- **What is wrong**: The behavioral spec says root execution must keep writing and reading the global discovery file at `get_user_home()/chat-server.json` (`design/spec/behavioral-spec.md:26-27,52`). The target architecture explicitly moves implicit discovery to `<project runtime root>/chat-servers/root/chat-server.json` and calls the end of the global `~/.meridian/chat-server.json` singleton part of the migration (`design/architecture/target-architecture.md:101-107,127-129,334-337`). Those cannot both be true.
- **Why it matters**: This is not a wording nit; it changes how root `chat ls/show/log/close` discover a server and whether discovery is per-user or per-project. Implementers cannot satisfy both artifacts, and tests/docs will drift depending on which one they follow.
- **Concrete fix direction**: Pick one discovery model and make the whole package agree on it. If project-scoped discovery is intended, rewrite B1.4/B2.1/B5.3 and the migration section to say that explicitly. If the user-home singleton must stay for root flows, the architecture needs to preserve that instead of relocating discovery under the project runtime tree.
- **Severity**: blocking

### 2. The recommended nested scope key is per parent spawn, not per chat server instance
- **What is wrong**: The architecture says nested scope identity should be the current `MERIDIAN_SPAWN_ID`, with state rooted at `chat-servers/spawn-<spawn_id>/` (`design/architecture/target-architecture.md:82-87,91-97,111-123,184-190`). But `MERIDIAN_SPAWN_ID` identifies the parent Meridian spawn, not an individual chat server instance. Two chat servers launched from the same nested spawn would share one runtime root and would therefore recover and manage the same `chats/` tree, which conflicts with the spec’s “server instance” isolation promises (`design/spec/behavioral-spec.md:20,34-38`).
- **Why it matters**: This reintroduces the exact recovery/discovery bleed the redesign is supposed to remove in a realistic case: one delegated agent or smoke harness launching more than one chat server from the same spawned process.
- **Concrete fix direction**: Either explicitly constrain the design to **one nested chat server per spawn** and enforce that with a lock/clear error, or introduce a true server-instance identifier and define how restart recovery finds the same scope. Do not keep “per server instance” language while keying isolation only by parent spawn id.
- **Severity**: blocking

## Major findings

### 3. The design depends on a missing `MERIDIAN_SPAWN_ID` error path that the behavioral spec never defines
- **What is wrong**: The target architecture says nested launch must fail with a targeted error when `MERIDIAN_DEPTH > 0` but `MERIDIAN_SPAWN_ID` is absent (`design/architecture/target-architecture.md:93-97`). The behavioral spec has no matching unwanted-behavior requirement; B1.1 currently reads as though every nested `meridian chat --headless` invocation succeeds (`design/spec/behavioral-spec.md:16`).
- **Why it matters**: This is a real edge case called out in the review prompt. Without an explicit requirement, implementers either invent behavior ad hoc or ship the architecture’s failure path while technically violating the EARS surface.
- **Concrete fix direction**: Add a dedicated EARS statement and verification row for this case, e.g. “If `MERIDIAN_DEPTH > 0` and `MERIDIAN_SPAWN_ID` is unavailable, then the system shall fail with a targeted error explaining that nested chat launch needs a stable ownership key.”
- **Severity**: major

### 4. B8.2 over-promises crash recovery relative to the proposed scope model
- **What is wrong**: B8.2 says a crashed nested server’s scoped state “shall be recoverable by a subsequent chat server start at any depth” (`design/spec/behavioral-spec.md:70`). Option B does not provide that. Recovery is tied to whichever scope the next process resolves, and the architecture explicitly keys scopes by root vs current `MERIDIAN_SPAWN_ID` and does not migrate or globally rediscover prior nested scopes (`design/architecture/target-architecture.md:91-97,125-134,334-347`). A root restart will use `chat-servers/root/`; a different nested spawn will use a different `spawn-<id>/`.
- **Why it matters**: Either the recovery guarantee is false or the architecture is incomplete. That is a correctness and testability problem for the crash-only story.
- **Concrete fix direction**: Narrow the requirement to recovery within the **same resolved scope**, or add a concrete cross-scope attach/recovery mechanism. Given the stated scope, narrowing the requirement is probably the right move.
- **Severity**: major

## Minor findings

### 5. Nested non-headless behavior is still ambiguous
- **What is wrong**: The requirements ask for nested chat startup to work for smoke testing, and the spec only explicitly allows nested `--headless` plus bans nested `--dev` and auto-open (`design/spec/behavioral-spec.md:16,74-76`). The architecture discusses nested launch generically and never says whether plain `meridian chat` with built frontend assets is supported or should fail (`design/architecture/target-architecture.md:203-210`).
- **Why it matters**: `_chat` defaults to non-headless today, so this ambiguity can produce materially different implementations and smoke guidance. A delegated caller that forgets `--headless` may either get a supported UI-serving server or an error depending on interpretation.
- **Concrete fix direction**: State this explicitly in the spec: either nested execution supports normal non-dev UI serving but never auto-opens a browser, or nested execution is headless-only and should fail fast without `--headless`.
- **Severity**: minor

## Verdict

**Request changes.**

Option B is the right class of fix; the simpler “remove the guard, skip discovery writes, require `--url`” alternative is **not** sufficient because `recover_all()` already mutates shared chat histories on startup, so recovery cross-talk is an immediate corruption risk, not a theoretical one. But before implementation starts, the team should resolve the discovery-path contradiction, decide whether isolation is per spawn or per server instance, and add the missing nested error/recovery semantics to the behavioral spec.
