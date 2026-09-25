# Attempt-fact contracts

`AttemptFacts` belongs to one attempt, not a connection or chat. The synchronous
emit hook updates it before any writer; a failed fold marks it incomplete and the
emit boundary logs the error without stopping delivery. Only the current reply is
retained; final text has a 1 MiB UTF-8 cap and an explicit truncation flag.

Finalization prefers explicit `report.md`, then attempt facts, then exact native
reply evidence. OpenCode V2 is the exception: its event-named native reply wins
over its stream text. An empty or unavailable exact lookup falls back to the
stream, not to another message or an ambient database.

Claude prefers result frames to assistant frames and sums model usage across
result frames. Codex keeps the latest cumulative usage and the final parent-thread
agent message; later command execution invalidates that message. Pi keeps the
latest assistant usage and agent-end text, plus typed failure evidence. OpenCode
V1 ignores child-session assistant parts. V2 binds `assistantMessageID` to
`session_message.id` within the recorded session/database. Unknown usage fields
stay `None`; an explicitly reported zero is preserved.

`detect_session_id_from_event` is separate from report extraction: Codex accepts
owned thread/session envelopes, Claude top-level owned frame IDs, and OpenCode
its session envelope fields. `conclude_native_run` observes the first attempt
signal and then the diagnostic connection-current ID; it does not scan artifacts.

Claude `--print` primaries are black-box processes. Their captured `output.jsonl`
stdout is folded after exit. Native TUI primaries without capture produce no
facts. Neither path reads runner history.
