# Pi spawn failure fixtures

Three failure modes for issue #262 (Pi spawn failure surfacing):

| Mode | Fixture | What happened |
|------|---------|---------------|
| **A — Prompt rejection** | `history_prompt_rejection.jsonl` | Pi emitted failed `response` on stdout (`success: false`); drain records the error but report extraction used to fall through to cleanup lifecycle JSON. |
| **B — Broken binary** | `history_broken_pi_lifecycle_only.jsonl` | Pi process died before any parseable stdout; history contains only Meridian lifecycle phases ending in `cleanup_completed`. |
| **C — Timeout / hang** | (covered by integration tests) | `first_pi_event_timeout` / `pi_rpc_no_response_after_initial_prompt`; no dedicated fixture file. |

Use these with `extract_or_fallback_report()` / `extract_pi_failure_from_history()` unit tests.
