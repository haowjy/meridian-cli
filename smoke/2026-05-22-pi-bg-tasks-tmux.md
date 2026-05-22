# Pi extensions — tmux live smoke (2026-05-22)

Worktree: `pi-generic-background-tasks`  
State dir: `$HOME/meridian-pi/smoke-pi-bg-tasks-20260522`  
Tmux session: `pi-smoke` (attach: `tmux attach -t pi-smoke`)

## Rebase on `origin/main` (2026-05-22)

- Branch rebased onto `94576a26` (`release: v0.2.0`), including **#252** (Pi harness, Mars launch bundle schema v2, model-optional passthrough) and **#253** (work dotfile filter).
- Dropped duplicate local commit `8d963af0` (already upstream).
- WIP stash applied cleanly — **no merge conflicts**.
- Post-rebase: `npm run build:extensions` OK; pytest `test_pi_projection` + `test_lifecycle_extension` **27 passed**.
- Live `meridian spawn --harness pi` now fails at Mars bundle with `pi_incompatible` unless Mars ≥ 0.6.1 is installed — environment follow-up, not a rebase conflict.

## Environment notes (required for this machine)

| Issue | Mitigation |
|-------|------------|
| `pi` crashes on Node 20 (`undici` / `markAsUncloneable`) | `export PATH="/home/jimyao/.nvm/versions/node/v24.13.0/bin:$PATH"` before `pi` or `meridian spawn --harness pi` |
| `-m gpt-5.4-mini` on Pi resolves to `azure-openai-responses` with no API key | Use **`-m openai-codex/gpt-5.4-mini`** for live Pi spawns (still “gpt-5.4-mini” family) |
| Bare `gpt-5.4-mini` dry-run defaults to **codex** harness, not pi | Always pass `--harness pi` for Pi RPC smoke |

Helper: `source smoke/pi-smoke-env.sh` from repo root.

## Pass/fail matrix

| Track | Step | Result | Notes |
|-------|------|--------|-------|
| **Prep** | `npm run build:extensions` | PASS | |
| **Prep** | `pi --version` (Node 24 PATH) | PASS | 0.75.4 |
| **Prep** | `meridian spawn --harness pi --dry-run` | PASS | Projects both extensions on RPC |
| **B** | `background_task` start + wait | PASS | Harness: `smoke/pi-extension-command-harness.mjs` |
| **B** | `/ps` notify rows | PASS | `[task] … exited …` lines |
| **B** | `/ps:logs` | PASS | `smoke-done` in combined.log tail |
| **B** | `/ps:clear` | PASS | `cleared N finished task(s)` |
| **B** | `/ps json` | PASS | `[]` after clear |
| **B** | `/spawns` (both ext loaded) | PASS | `No spawns in tree.` |
| **B** | Interactive `pi -e …` TUI panels | SKIP | Notify-only UI; pi-processes panels not ported |
| **C** | S1 minimal spawn (`gpt-5.4-mini` token) | **FAIL** | p2205 — Pi never responded; default Node 20 + azure auth |
| **C** | S1 minimal spawn (`openai-codex/gpt-5.4-mini`) | PASS | p2206 succeeded 3.2s, report `SMOKE_OK` |
| **C** | S6 tracked `background_task` | PASS | p2207 succeeded 16.3s; report mentions `child-done` |
| **C** | Sidecar `meridian.quiescence.ready` | PASS | p2207 `pi-lifecycle-events.jsonl` |
| **C** | Task state on disk | PASS | `background-tasks/<pi-session>/tasks/t-mphirqwj-nul5rc/combined.log` → `child-done` |
| **C** | `meridian.subspawn.*` in sidecar file | PARTIAL | Sidecar file only had quiescence line; subspawn may be in harness `history.jsonl` only |
| **A** | Native projection loads spawn-watch only | PASS | pytest `test_pi_native_projection_loads_lifecycle_extension_only` |
| **A** | `/ps` on spawn-watch-only load | PASS (absent) | Commands: `spawns`, `spawns:*` only; `has ps: false` |
| **A** | `/spawns` notify | PASS | `No spawns in tree.` |
| **A** | Interactive `meridian --harness pi` TUI | SKIP | Not run interactively; projection + harness cover command registration |

## Sample output — Track B (`/ps` notify)

From `node smoke/pi-extension-command-harness.mjs both`:

```text
[task] t-mphir0m3-dwydg2 exited smoke-task pid=232219
--- .../combined.log ---
smoke-done

cleared 3 finished task(s)
[]
No spawns in tree.
```

## Sample output — Track C (spawns)

```text
$ uv run meridian spawn --harness pi -m openai-codex/gpt-5.4-mini -p "Reply with exactly: SMOKE_OK"
p2206 succeeded (3.2s)
# Auto-extracted Report
SMOKE_OK

$ uv run meridian spawn --harness pi -m openai-codex/gpt-5.4-mini -p 'Use background_task ... sleep 3 && echo child-done ...'
p2207 succeeded (16.3s)
```

Sidecar tail (p2207):

```json
{"type":"meridian.quiescence.ready","schema_version":1,"session_id":"019e51e7-ea6d-7d1c-8f2b-526d42eee0d3","parent_spawn_id":"p2207","role":"spawned","tracked_count":0,"pending_notification_count":0}
```

Task log:

```text
child-done
```

## Sample output — Track A (native extension set)

Spawn-watch-only registration:

```text
commands: spawns, spawns:cancel, spawns:clear, spawns:logs, spawns:show, spawns:wait
has ps: false
NOTIFY: No spawns in tree.
```

RPC spawn projection (from p2207 history) loads both:

```text
-e .../background-tasks/index.js
-e .../meridian-spawn-watch/index.js
```

## UX findings

1. **Slash commands render as `ui.notify` text**, not pi-processes panels (expected per v1 scope).
2. **Native primary omits `background-tasks`** — `/ps` unavailable on `meridian --harness pi`; use spawned RPC or manual `pi -e` both extensions.
3. **Spawn tree empty** for pure `background_task` runs (no `meridian spawn` wrapper); tree fills when wrapper/spawn detection runs.

## Follow-ups

- Document Node ≥22 (24 recommended) in Pi smoke prerequisites.
- Clarify model token for Pi: `openai-codex/gpt-5.4-mini` vs bare `gpt-5.4-mini`.
- Optional: load `background-tasks` on native primary if product wants `/ps` in `meridian --harness pi`.
- Port pi-processes TUI when `@earendil-works/pi-tui` peer deps are added.

## Artifacts

| Path | Purpose |
|------|---------|
| `smoke/pi-smoke-env.sh` | Env for tmux panes |
| `smoke/pi-extension-command-harness.mjs` | Non-interactive `/ps` + tool smoke |
| `/tmp/pi-harness-both.log` | Harness JSON output |
| `/tmp/smoke-s1-codex.log` | S1 success log |
| `/tmp/smoke-s6.log` | S6 success log |
