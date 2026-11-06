# Testing

Claude runtime changes require local `claude plugin validate` and `claude plugin
test`; CI has no Claude binary. Pi runtime changes require `pnpm run
verify:extensions` in `src/meridian/pi_runtime`.

A nonzero subprocess exit becomes a `meridian/error/connectionClosed` event
before the stream ends. Fakes that skip that event miss exit precedence. Native
continue/fork fixtures need real headers matching their recorded identity, or
locator validation rejects them before the behavior under test.

`uv run pytest --runner-history=off <tests>` traps implicit reads of legacy
runner `history.jsonl`, including in CLI subprocesses.
