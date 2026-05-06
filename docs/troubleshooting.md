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
export MERIDIAN_WAIT_YIELD_AFTER_SECONDS=240
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

To resume a prior spawn:
```bash
meridian spawn --continue ID -p "continue from where you left off"
```

To start a new spawn with a prior spawn or chat/session as context:
```bash
meridian spawn --from REF -p "next task"
```

Use a spawn ref such as `p123` to include that spawn's report/files. Use a chat
ref such as `c123` to point at the exact session transcript and primary spawn.

To find which spawns belong to a work item:
```bash
meridian work                      # dashboard with attached spawns
meridian report search "keyword"   # search across all spawn reports
```

## Spawn shows as `finalizing`

`finalizing` is a normal, short-lived active status. It means the runner has finished its post-exit work (output drain, report extraction) and is committing the terminal state. You may briefly see it in `spawn list` or the `work` dashboard between harness exit and terminal persistence. No action needed — the spawn will move to `succeeded` or `failed` momentarily.

If a spawn stays in `finalizing` for more than a minute or two, the runner may have crashed in the finalization window. In that case `meridian doctor` will reclassify it (see below).

## Spawn shows as orphaned

Meridian classifies a spawn as orphaned when its runner process is gone and there has been no recent activity on the spawn's artifacts (heartbeat, output, stderr, or report) for 120 seconds. There are two distinct orphan errors:

- **`orphan_run`** — the spawn record was `status=running` (or `queued`) when reaped. The runner died before completing post-exit work; because output drain and report extraction happen while status is still `running`, a crash during drain also produces this error. The spawn likely produced partial or no output.
- **`orphan_finalization`** — the spawn record was `status=finalizing` when reaped, meaning the runner completed all post-exit work but crashed in the narrow window before persisting the terminal state. The spawn is likely to have a usable `report.md` on disk even though it was classified as failed.

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

`meridian doctor` surfaces workspace findings as distinct codes. Fix new workspace config by editing `[workspace.NAME]` entries in `meridian.toml` or `meridian.local.toml`; use `meridian workspace init` to scaffold local examples. If you still have legacy `workspace.local.toml`, run `meridian workspace migrate` to convert it to `[workspace]` entries in `meridian.local.toml`.

### `workspace_invalid`

The workspace config is invalid. Causes include invalid TOML, a `[workspace.NAME]` entry with a missing/empty/non-string `path`, an invalid entry name, scalar values directly under `[workspace]`, or a workspace config path that is a directory rather than a file.

Fix the TOML or schema error in `meridian.toml` / `meridian.local.toml`, then rerun `meridian doctor`.

**An invalid workspace blocks spawns.** Launches fail before contacting any harness until the config is fixed or removed.

### `workspace_unknown_key`

A workspace entry contains keys Meridian doesn't recognize. Forward-compatibility warning only — does not block launches. Safe to ignore if the key is intentional (written by a newer Meridian version). Otherwise, remove or rename the key.

### `workspace_local_missing_root`

A local `[workspace.NAME]` entry in `meridian.local.toml` points to a path that does not exist as a directory. The root is skipped at launch time and produces no projection.

Check the entry name and path in `meridian.local.toml`. Relative workspace paths resolve against the project root. Use an absolute path if the local checkout is outside the standard repo layout.

Committed workspace entries in `meridian.toml` behave differently: missing committed paths are silently skipped because they usually mean this machine has a partial checkout.

### `workspace_legacy_file_present`

A legacy `workspace.local.toml` file exists. During the migration period Meridian may read it only as a fallback when no `[workspace]` entries exist in `meridian.toml` or `meridian.local.toml`; otherwise it is ignored.

Run:

```bash
meridian workspace migrate
```

If `meridian.local.toml` already contains `[workspace]` entries, migration aborts to avoid overwriting local configuration. Use `meridian workspace migrate --force` only when you intend to replace existing local workspace entries with migrated legacy roots. Review the generated names after migration; they are basename-derived and may not match future committed convention names.

### `workspace_unsupported_harness`

Workspace roots could not be projected to the selected harness. The spawn proceeds, but that harness won't see the declared roots.

If multi-repo filesystem access is required, use a harness that supports workspace projection for your setup.

## Spawn artifacts

Each spawn writes artifacts to the user-level runtime directory, under `~/.meridian/projects/<uuid>/spawns/<spawn_id>/` on POSIX (or `%LOCALAPPDATA%\meridian\projects\<uuid>\spawns\<spawn_id>\` on Windows). Use `meridian spawn show ID` to read them without navigating the path directly.

| File | Contents |
| ---- | -------- |
| `state.json` | Authoritative spawn record (status, metrics, error) |
| `report.md` | Agent's final report |
| `output.jsonl` | Raw harness output (surfaced through `meridian session log <spawn_id>`) |
| `stderr.log` | Harness stderr, warnings, errors |
| `system-prompt.md` | System instruction content as sent to the harness (Claude composed launches) |
| `starting-prompt.md` | Full user-turn content (prompt + prepended context) |
| `projection-manifest.json` | Harness ID and per-category channel routing decisions |

If a spawn directory is missing entirely, the harness crashed before artifacts stabilized — relaunch.

## Slow first spawn after upgrade

After upgrading to a version that uses the v2 spawn state format, the first `meridian spawn` or `meridian spawn list` call triggers a one-time migration. Meridian reads the legacy `spawns.jsonl` event log, writes a `state.json` file for each existing spawn, and atomically archives the old file to `spawns.legacy-v1.jsonl`.

**This is automatic — no user action required.** Migration time scales with spawn history: a few seconds for small histories, up to 2–3 minutes for very large ones. After migration, spawn operations (including primary launch) are significantly faster.

Once migration completes, `spawns.legacy-v1.jsonl` is safe to delete if you want to reclaim space. It is not read after migration.

## Stale state accumulating in `~/.meridian/`

Over time, orphan project directories and old spawn artifacts accumulate under `~/.meridian/projects/`. Per-project orphan repairs (stale locks, orphaned runs) happen silently in the background on each launch. Use `meridian doctor` to inspect and clean up manually.

To inspect what's stale:

```bash
meridian doctor           # per-project scan (cheap, run from anywhere)
meridian doctor --global  # cross-project scan — must run from the root process, not inside a spawn
```

To clean up:

```bash
meridian doctor --prune           # prune stale spawn artifacts (current project)
meridian doctor --prune --global  # also prune orphan project dirs machine-wide
```

Pruning respects `state.retention_days` (default 30 days). Configure in `meridian.toml`:

```toml
[state]
retention_days = 30   # -1 = never prune, 0 = prune immediately
```

Or via environment variable: `MERIDIAN_STATE_RETENTION_DAYS=30`.

Active spawns are always protected — pruning never deletes state for running spawns regardless of age.
