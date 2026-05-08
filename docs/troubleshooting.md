# Troubleshooting

## Spawns waste tokens on cache misses

If long-running spawns feel expensive or slow to resume, the harness prompt cache may be going cold while Meridian waits.

**Root cause:** Meridian's default `spawn wait` yield interval is 50 minutes. If the harness's prompt-cache TTL is shorter than that, the next turn reprocesses the full context from scratch.

| Harness   | Cache TTL | Fix |
|-----------|-----------|-----|
| Claude Code | 5 minutes (default) or 1 hour (subscription / `ENABLE_PROMPT_CACHING_1H=1`) | Extend to 1 hour via `~/.claude/settings.json` (see [Getting Started](getting-started.md)) |
| Codex CLI | 5–10 minutes typical, up to 1 hour extended | Usually automatic on current models |
| OpenCode | Harness-dependent | Check OpenCode docs for cache behavior |

**Quick workarounds:**

```bash
# Lower yield for a single wait
meridian spawn wait --yield-after-secs 240

# Global override
export MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS=240
```

## `meridian` not found

Run `uv tool update-shell` and restart your shell. If using a virtual environment, activate it first.

## Harness not found

`meridian doctor` reports missing harnesses when the harness binary is not on `$PATH`.

Install the missing harness:
- Claude Code: [docs.anthropic.com](https://docs.anthropic.com/en/docs/claude-code)
- Codex CLI: [github.com/openai/codex](https://github.com/openai/codex)
- OpenCode: [opencode.ai](https://opencode.ai)

Then confirm with `meridian doctor`.

## Model routes to wrong harness

Harness routing is determined by model prefix patterns. Check what's resolved:

```bash
meridian mars models list # see available models and their harnesses
meridian config show      # see harness defaults and overrides
```

To force a specific harness for a spawn, use `--harness`:

```bash
meridian spawn -m MODEL --harness claude -p "task"
```

Set a default harness for a model family in `meridian.toml`:

```toml
[harness]
claude = "claude-opus-4-6"
codex  = "gpt-5.3-codex"
```

## `meridian codex` feels slow at startup

Fresh managed Codex startup is slower than a black-box TUI launch because Meridian must start Codex `app-server`, connect the managed observer, create the thread, materialize the first rollout, and only then attach the real Codex TUI.

This is expected for the managed path. Meridian now shows compact startup telemetry so the delay is visible instead of looking hung.

See [codex-tui-passthrough.md](codex-tui-passthrough.md) for the startup phases and bootstrap rationale.

## Codex managed attach fails instead of falling back

Codex primary is intentionally managed-only. Meridian does not silently fall back to black-box Codex for `meridian codex`, because hidden instruction routing and managed session tracking are the point of that command.

If managed startup fails:

```bash
meridian spawn show ID
meridian session log ID
```

Also inspect the spawn's `stderr.log` artifact if needed. Common failure surfaces are:

- Codex `app-server` startup failure
- observer connection failure
- bootstrap turn failure before TUI attach
- local TUI attach failure after the managed thread is ready

## Spawn disconnected from earlier work

Primary session resume/fork:
```bash
meridian --continue REF
meridian --fork REF
```

Child spawn resume/fork/context attach:
```bash
meridian spawn --continue ID -p "continue from where you left off"
meridian spawn --fork REF -p "start a new branch of this spawn/chat context"
meridian spawn --from REF -p "next task"
```

Use spawn refs such as `p123` or chat/session refs such as `c123`.

Primary `--continue REF` / `--fork REF` and spawn `--fork REF` also accept a raw harness session id, for example:
```bash
meridian --continue 01JABCDEF1234567890
```

When a primary session ends, the quit message now shows the chat ID (e.g. `c123`) as the preferred `--continue` reference. Chat IDs are stable and human-friendly; UUIDs still work but are no longer the default suggestion.

`--continue` resumes the same session. `--fork` starts a new session seeded from prior context. `--from` only attaches prior context; it does not resume/fork transcript state.

To find which spawns belong to a work item:
```bash
meridian work                      # dashboard with attached spawns
meridian spawn report search "keyword"   # search across all spawn reports
```

## Spawn shows as `finalizing`

`finalizing` is a normal, short-lived active status. It means the runner has finished its post-exit work (output drain, report extraction) and is committing the terminal state. You may briefly see it in `spawn list` or the `work` dashboard between harness exit and terminal persistence. No action needed — the spawn will move to `succeeded` or `failed` momentarily.

If a spawn stays in `finalizing` for more than a minute or two, the runner may have crashed in the finalization window. In that case `meridian doctor` will reclassify it (see below).

## Spawn shows as orphaned

Meridian classifies a spawn as orphaned when its runner process is gone and there has been no recent activity on the spawn's artifacts (heartbeat, output, stderr, or report) for 120 seconds. There are two distinct orphan errors:

- **`orphan_run`** — the spawn record was `status=running` (or `queued`) when reaped. The runner died before completing post-exit work; because output drain and report extraction happen while status is still `running`, a crash during drain also produces this error. The spawn likely produced partial or no output.
- **`orphan_finalization`** — the spawn record was `status=finalizing` when reaped, meaning the runner completed all post-exit work but crashed in the narrow window before persisting the terminal state. The spawn is likely to have a usable `report.md` on disk even though it was classified as failed.
- **`launch_boundary_no_takeover`** — a background spawn's `launch-boundary.jsonl` shows that the parent launched the subprocess but the worker process never recorded a takeover event. The harness process died in the startup window before it could begin work. No output was produced.
- **`orphan_primary`** — a managed Codex/OpenCode primary lost its Meridian launcher/wrapper. Passive reconciliation records the failed state but does not signal managed-primary runtime children such as backend app servers or attached TUIs. Use `meridian spawn cancel ID` when you explicitly want Meridian to clean up tracked leftovers.

To detect and reconcile orphaned state, run:

```bash
meridian doctor
```

After reconciliation, inspect the spawn:

```bash
meridian spawn show ID          # check status, report, and error field
```

If `report.md` exists and looks complete, the work product is likely usable even though the spawn is marked `failed`. Relaunch only if the work wasn't done.

### A spawn briefly showed orphaned but now shows `succeeded`

This is expected, not a bug. Meridian's read-path reconciler makes a best-effort assessment based on heartbeat and artifact recency. If the runner was slow (not dead) and later completed normally, its terminal status overwrites the reconciler's orphan stamp — the process that actually ran the work has final say. You can confirm by checking `meridian spawn show ID`.

## Spawn exited with code 143 or 137

The process was killed externally (SIGTERM/SIGKILL). Check `meridian spawn show ID` — if status is `succeeded`, the signal hit during cleanup and no retry is needed. Otherwise check for OOM or external kill, then retry.

## Config not taking effect

Config resolution precedence: CLI flag > ENV var > YAML profile > project config > user config > harness default.

Verify what's actually resolved for a field:
```bash
meridian config show
```

A CLI `-m MODEL` override must also drive harness selection — a profile-level harness default cannot win over a CLI model override.

## Workspace issues

`meridian doctor` surfaces workspace findings as distinct codes. Fix workspace config by editing `[workspace.NAME]` entries in `meridian.toml` or `meridian.local.toml`; use `meridian workspace init` to scaffold local examples.

### `workspace_invalid`

The workspace config is invalid. Causes include invalid TOML, a `[workspace.NAME]` entry with a missing/empty/non-string `path`, an invalid entry name, scalar values directly under `[workspace]`, or a workspace config path that is a directory rather than a file.

Fix the TOML or schema error in `meridian.toml` / `meridian.local.toml`, then rerun `meridian doctor`.

**An invalid workspace blocks spawns.** Launches fail before contacting any harness until the config is fixed or removed.

### `workspace_unknown_key`

A workspace entry contains keys Meridian doesn't recognize. Forward-compatibility warning only — does not block launches. Safe to ignore if the key is intentional (written by a newer Meridian version). Otherwise, remove or rename the key.

### `workspace_local_missing_root`

A local `[workspace.NAME]` entry in `meridian.local.toml` points to a path that does not exist as a directory. The root is skipped at launch time and produces no projection.

Check the entry name and path in `meridian.local.toml`. Relative workspace paths resolve against the project root. Use an absolute path if the local checkout is outside the standard repo layout.

Committed workspace entries in `meridian.toml` behave differently: missing committed paths are skipped from projection and surfaced as `workspace_missing_root` findings (warnings), which usually indicate a partial checkout on this machine.

### `workspace_unsupported_harness`

Workspace roots could not be projected to the selected harness. The spawn proceeds, but that harness won't see the declared roots.

If multi-repo filesystem access is required, use a harness that supports workspace projection for your setup.

## Spawn artifacts

Each spawn writes artifacts to the user-level runtime directory, under `~/.meridian/projects/<uuid>/spawns/<spawn_id>/` on POSIX (or `%LOCALAPPDATA%\meridian\projects\<uuid>\spawns\<spawn_id>\` on Windows). Use `meridian spawn show ID` to read them without navigating the path directly.

| File | Contents |
| ---- | -------- |
| `state.json` | Authoritative spawn record (status, metrics, error) |
| `report.md` | Agent's final report |
| `history.jsonl` | Raw harness event log (surfaced through `meridian session log <spawn_id>`); `output.jsonl` may still appear as a legacy fallback |
| `stderr.log` | Harness stderr, warnings, errors |
| `launch-boundary.jsonl` | Background spawn startup lifecycle — parent-side launch attempt/spawned/failed events and worker-side boot/takeover events. Used by the reaper to detect startup-phase failures. |
| `system-prompt.md` | System instruction content as sent to the harness (Claude composed launches) |
| `starting-prompt.md` | Full user-turn content (prompt + prepended context) |
| `projection-manifest.json` | Harness ID and per-category channel routing decisions |

If a spawn directory is missing entirely, the harness crashed before artifacts stabilized — relaunch.

## Slow first spawn after upgrade

After upgrading to a version that uses the v2 spawn state format, the first `meridian spawn` or `meridian spawn list` call triggers a one-time migration. Meridian reads the legacy `spawns.jsonl` event log, writes a `state.json` file for each existing spawn, and atomically archives the old file to `spawns.legacy-v1.jsonl`.

**This is automatic — no user action required.** Migration time scales with spawn history: a few seconds for small histories, up to 2–3 minutes for very large ones. After migration, spawn operations (including primary launch) are significantly faster.

Once migration completes, `spawns.legacy-v1.jsonl` is safe to delete if you want to reclaim space. It is not read after migration.

## Claude session isolation

Claude stores per-session transcript files under the configured Claude config root (default `~/.claude`) in `projects/<project-slug>/`. Meridian gives each Claude session (primary session or child spawn) its own isolated config directory — an **overlay** — so its `projects/` directory starts empty and concurrent sessions can't bleed conversation context into each other.

**What the overlay contains**

The overlay lives inside Meridian's runtime state at `~/.meridian/projects/<uuid>/claude-config/<spawn_id>/` (POSIX) or `%LOCALAPPDATA%\meridian\projects\<uuid>\claude-config\<spawn_id>\` (Windows). It mirrors most of the configured Claude config root (default `~/.claude`) — credentials, settings, extensions — but gives Claude a fresh empty `projects/` directory for each Claude session. Mutable user-state files (`.claude.json`, `.credentials.json`, `statsig`, `memory`, `cached_preferences`, `todos`) are copied in; read-only entries are linked or copied depending on platform (for example, symlinks on POSIX). Overlay directories are internal implementation state; don't read or modify them directly.

**Primary and child spawns**

Both primary sessions (`meridian`) and child spawns (`meridian spawn`) use isolated overlays. Child spawns also have Claude's parent-session sentinel (`CLAUDECODE`) cleared, allowing Claude to run without nesting restrictions under Meridian's control.

When a primary session or child spawn uses `--continue`, `--fork`, or equivalent session-resume flows, Meridian seeds the target overlay's `projects/` directory from the source session transcript so Claude can resume prior context. Plain `--from` context attachment does not trigger transcript seeding. Seeding only runs when Meridian can resolve both the source session ID and source execution CWD.

**Session transcripts after normal exit**

When a primary session or child spawn exits normally, Meridian attempts to materialize its session transcript files from the overlay's `projects/` directory into the canonical Claude `projects/` tree under the configured Claude config root (default `~/.claude/projects/`). This means conversations from Meridian-managed Claude sessions usually appear in your regular Claude history after they complete. Meridian also preserves selected auth/config files (`.claude.json` and `.credentials.json`) back to the durable Claude config root so overlay-side login or token refresh changes are not discarded. This does not repair already-invalid durable Claude credentials; if raw `claude` reports `401 Invalid authentication credentials`, re-authenticate with Claude directly first. After these best-effort materialization steps, Meridian removes the overlay immediately on normal completion. `doctor --prune` only cleans up stale/crash-orphaned overlays left behind when normal cleanup did not finish.

**Reading spawn conversations**

`meridian session log <spawn_id>` may read Meridian spawn artifacts (`history.jsonl` / legacy `output.jsonl`) for active or fallback paths, and otherwise reads native harness transcripts. For Claude, those native transcripts resolve under Claude's session-store `projects/` tree.

## Stale state accumulating in `~/.meridian/`

Over time, orphan project directories, old spawn artifacts, stale crash-orphaned Claude overlay directories, and expired telemetry segments accumulate under `~/.meridian/`. Per-project orphan repairs (stale locks, orphaned runs) happen silently in the background on each launch. Use `meridian doctor` to inspect and clean up manually.

To inspect what's stale:

```bash
meridian doctor           # per-project scan (cheap, run from anywhere)
meridian doctor --global  # same checks + machine-wide orphan-project-dir scan; must run from the root process
```

`meridian doctor` reports stale Claude overlay directories as `stale_claude_overlays` and telemetry cleanup candidates as `stale_telemetry_segments`. That telemetry warning can mean expired segments, total telemetry size above the cap, or both. Stale overlay directories are safe to keep until pruned — they may contain transcripts, any transcripts inside remain until deletion, and successful materialization may also create a canonical copy.

To clean up:

```bash
meridian doctor --prune           # prune stale spawn artifacts, stale Claude overlays, and telemetry retention targets (current project only)
meridian doctor --prune --global  # also prune orphan project dirs machine-wide
```

When `--prune` removes a stale Claude overlay, it first makes a best-effort attempt to materialize any session transcript files into the canonical Claude `projects/` tree (usually `~/.claude/projects/`). Meridian then deletes the overlay directory and reclaims disk space. If transcript materialization succeeds and overlay deletion succeeds, Meridian also repairs spawn/session metadata to point to the canonical config root. If materialization fails for a particular overlay, Meridian logs a warning and continues prune handling. Telemetry pruning is current-project only: it removes expired non-live segments first, then may remove older closed segments if needed to get back under the telemetry size cap.

Pruning respects `state.retention_days` (default 30 days). Configure in `meridian.toml`:

```toml
[state]
retention_days = 30   # -1 = never prune, 0 = prune immediately
```

Or via environment variable: `MERIDIAN_STATE_RETENTION_DAYS=30`.

Active spawns are always protected — pruning never deletes state for running spawns regardless of age.
