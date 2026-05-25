# extensions/background-tasks/

Generic OS background task registry, `background_task` tool, unified `/ps*` UI, and in-session Meridian spawn rows.

State: `{MERIDIAN_PI_STATE_DIR}/background-tasks/{sessionId}/tasks/{task_id}/`.

## Slash command convention

- **`/ps`** — task overlay (interactive), or table output when headless. **`/ps json`** is the only space-arg mode on the parent command (structured dump).
- **`/ps:<verb>`** — task actions (Pi colon namespace, same family as skills): `ps:b`, `ps:background`, `ps:kill`, `ps:logs`, `ps:clear`, `ps:pin`, `ps:dock`, `ps:settings`.
- **`/mspawn:wait <p-id>`** — runs `meridian spawn wait <id>` (not part of `/ps`; replaces removed `/spawns:wait`).
- **Overlay keys** — Meridian UI when `/ps` is open (`b`, `j/k`, `x`, `c`, …); not Pi core.

## Interactive `$` and /ps

- Pi **`!`** is a run shortcut — not background.
- Trailing **`&`** on **`$`** (`splitUserBashBackground`): immediate `{ result }`, detached task in registry/`/ps`, no `waitForCompletion`.
- Foreground **`$`**: `operations.exec` blocks until exit or background; foreground row pinned with **`● fg`**.
- **`/ps:b`** or **`/ps:background`**: backgrounds the blocking foreground **`$`** without opening the overlay — `backgroundForegroundBash` → `releaseWait` + clear foreground slot; exec exits `0` with `Sent to background — /ps`.
- **In-chat hint** (`meridian:foreground-bash-hint`): once per foreground **`$`** task — `/ps to manage tasks · /ps:b to run in background`. User-only (`details.hintText`, empty `content`, `triggerTurn: false`). Not for trailing **`&`** (already detached).
- **`/ps` overlay**: **`b`** on foreground row → same background path. Footer shows **`b` background** and **`/ps:b`** when applicable.
- Agent **`bash`**: lifecycle via `tool_result` only (no `&` auto-background).
- **Status widget** (below editor): live tasks only — `tasks: <name> running | +N more`. Hidden while `/ps` is open.
- **`/ps`**: `TasksPanelComponent` returned directly from `ui.custom` overlay — **never** call `tui.setFocus` (Pi `showOverlay` owns `preFocus`). Layout in `panel/ps-view.ts` (preview top, list + footer bottom). **1 row per task** (max 8); **4-line** preview. **Enter** → log stream overlay; **q** back. **1s** timer + **`meridian:task:output`** for live preview.

## Meridian spawn rows (`src/spawn/`)

**Source of truth:** persisted spawn records on disk — what `meridian spawn wait` polls. Pi does not classify spawns from the shell command string.

| Step | Mechanism |
|------|-----------|
| Discover ids | `meridian --json spawn list` (chat-scoped) on task end / periodic refresh |
| Log fallback | Parse `pNNNN` from task combined log, then still confirm |
| Confirm + status | `meridian --json spawn show` per id |
| `/ps` row | `meridian:spawn:*` bus → `meridian_spawn` kind in `unified_rows.ts` |

Shell wrappers (`meridian spawn …`) stay **`process`** rows until a confirmed spawn id exists. Active statuses polled: `queued`, `running`, `finalizing`.

**Wait:** use **`meridian spawn wait`** in the terminal or **`/mspawn:wait <p-id>`** in Pi (30m subprocess cap). **`/ps` has no wait** subcommand. Wave notifications and `/spawns*` are removed.

Consumes and emits `meridian:spawn:*` on Pi's shared `pi.events` bus (via `resolveExtensionBus`).

## Verify after each implementation pass

1. `cd src/meridian/pi_runtime && npm run verify:extensions` (build + vitest + bundle smoke).
2. Delegate **smoke-tester** (verify mode) for `meridian pi` / RPC claims — mandatory for keyboard and spawn integration.
