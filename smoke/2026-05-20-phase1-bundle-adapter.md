# Phase 1 smoke: bundle adapter routing

Date: 2026-05-20
Worktree: `/home/jimyao/gitrepos/meridian-cli.worktrees/mars-capability-cache-resolver`

## Required exit-gate commands

### 1) `uv run meridian spawn -m haiku --dry-run --json`

```json
{"error":"prompt stdin is empty","exit_code":1}
```

### 1a) Same command with prompt payload (`"probe"`) to verify routing path

```json
{"harness_id":"claude","model":"claude-haiku-4-5","model_selection":{"requested_token":"haiku","canonical_model_id":"claude-haiku-4-5","harness_provenance":"provider"},"status":"dry-run"}
```

### 2) `uv run meridian spawn -m gpt-5.5 --harness opencode --dry-run --json`

```json
{"error":"prompt stdin is empty","exit_code":1}
```

### 2a) Same command with prompt payload (`"probe"`) to verify harness-model threading

```json
{"harness_id":"opencode","model":"gpt-5.5","cli_command":["opencode","run","--model","openai/gpt-5.5","-"],"model_selection":{"requested_token":"gpt-5.5","canonical_model_id":"gpt-5.5","harness_provenance":"cli"},"status":"dry-run"}
```

## Dry-run routing across harnesses

Command pattern used:

```bash
uv run meridian spawn "probe" -m gpt-5.4-mini --harness <harness> --dry-run --json
```

### claude

```json
{"harness_id":"claude","model":"gpt-5.4-mini","status":"dry-run"}
```

### codex

```json
{"harness_id":"codex","model":"gpt-5.4-mini","status":"dry-run"}
```

### opencode

```json
{"harness_id":"opencode","model":"gpt-5.4-mini","cli_command":["opencode","run","--model","openai/gpt-5.4-mini","-"],"status":"dry-run"}
```

### cursor

```json
{"error":"'cursor' is not a valid HarnessId","exit_code":1}
```

### pi

```json
{"error":"Missing Pi extension artifact: .../src/meridian/pi_runtime/dist/extensions/managed-bash/index.js. Build Pi extensions first (... npm run build:extensions).","exit_code":1}
```

## Verification commands

- `uv run ruff check .` ✅
- `uv run pyright` ✅
- `uv run pytest-llm` ✅ (`1356 passed, 11 skipped`)
