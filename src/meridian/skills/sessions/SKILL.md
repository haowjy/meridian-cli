---
name: sessions
description: When decisions or context must be recovered from past sessions.
---

# Session Mining

Design decisions, rejected alternatives, and discovered constraints often live
in conversation instead of code or docs. Compaction makes that context
expensive to recover later, so mine it before the next implementation or
documentation step starts.

## Recover From the Top-Level Session First

Start from the top-level conversation, regardless of how deep in the delegated
run tree you are. Its decisions and framing are usually the highest-leverage
read.

Read recent history first, then widen only when needed. Search instead of
paging when you know the decision, term, or file you need. When compaction has
split a transcript into segments, stay within the relevant segment so entry
references remain stable, and include its setup or handoff when the missing
context may have arrived there.

## Delegate Bulk Reading

When the question spans long histories or multiple sessions, delegate mining
and synthesis to `@session-miner`. That keeps your context focused on decisions
instead of raw transcript paging. If you are `@session-miner`, mine directly
rather than delegating recursively.

When work spans interruptions, reopenings, or multiple days, list every session
related to the work item before mining. Reading only the current session misses
prior decisions.

Skip mining if the handoff or current design artifacts already contain the
needed rationale. Skim artifacts first and mine only the gaps.

