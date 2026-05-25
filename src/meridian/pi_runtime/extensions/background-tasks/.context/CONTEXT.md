# extensions/background-tasks/

Generic OS background task registry, `background_task` tool, unified `/ps*` UI (pi-processes parity).

State: `{MERIDIAN_PI_STATE_DIR}/background-tasks/{sessionId}/tasks/{task_id}/`.

## Interactive `$` and /ps

- Pi **`!`** is a run shortcut — not background.
- Trailing **`&`** on **`$`** (`splitUserBashBackground`): immediate `{ result }`, detached task in registry/`/ps`, no `waitForCompletion`.
- Foreground **`$`**: `operations.exec` blocks until exit or background; foreground row pinned with **`● fg`**.
- **`/ps b`** or **`/ps background`** (space args on `ps`, like `json`): backgrounds the blocking foreground **`$`** without opening the overlay — `backgroundForegroundBash` → `releaseWait` + clear foreground slot; exec exits `0` with `Sent to background — /ps`.
- **In-chat hint** (`meridian:foreground-bash-hint`): once per foreground **`$`** task — `/ps to manage tasks · /ps b to run in background`. User-only (`details.hintText`, empty `content`, `triggerTurn: false`). Not for trailing **`&`** (already detached).
- **`/ps` overlay**: **`b`** on foreground row → same background path. Footer shows **`b` background** and **`/ps b`** when applicable.
- Agent **`bash`**: lifecycle via `tool_result` only (no `&` auto-background).
- **Status widget** (below editor): live tasks only — `tasks: <name> running | +N more`. Hidden while `/ps` is open.
- **`/ps`**: `TasksPanelComponent` returned directly from `ui.custom` overlay — **never** call `tui.setFocus` (Pi `showOverlay` owns `preFocus`). Layout in `panel/ps-view.ts` (preview top, list + footer bottom). **1 row per task** (max 8); **4-line** preview. **Enter** → log stream overlay; **q** back. **1s** timer + **`meridian:task:output`** for live preview.

Consumes `meridian:spawn:*` on Pi's shared `pi.events` bus (via `resolveExtensionBus`) for unified `/ps` rows.

## UX reference

Clone and refresh before parity work:

```bash
# already at ~/gitrepos/ref/pi-processes — git pull --ff-only
```

Map: work dir `pi-processes-parity-map.md` (meridian-cli work item). Pair with `meridian-spawn-watch` for `/spawns` tree.

## Verify after each implementation pass

1. `cd src/meridian/pi_runtime && npm run verify:extensions` (build + vitest + bundle smoke).
2. Delegate **smoke-tester** (verify mode) for `meridian pi` / RPC claims — mandatory for keyboard and spawn integration.
3. Diff touched UI/commands against the same path under `ref/pi-processes/src/`.
