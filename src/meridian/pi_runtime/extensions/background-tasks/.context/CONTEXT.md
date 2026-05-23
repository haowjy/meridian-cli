# extensions/background-tasks/

Generic OS background task registry, `background_task` tool, unified `/ps*` UI (pi-processes parity).

State: `{MERIDIAN_PI_STATE_DIR}/background-tasks/{sessionId}/tasks/{task_id}/`.

## Interactive `$` and /ps

- Pi **`!`** is a run shortcut — not background.
- Trailing **`&`** on **`$`** (`splitUserBashBackground`): immediate `{ result }`, detached task in registry/`/ps`, no `waitForCompletion`.
- Foreground **`$`**: `operations.exec` blocks until exit or background; foreground row pinned with **`● fg`**.
- **`ctrl+b`** (single press, only while foreground `$` runs): same as `/ps` background via `onTerminalInput` — Claude Code parity. Hint: **`(ctrl+b to run in background)`** in status widget + footer while foreground bash runs (Pi core still shows escape/ctrl+c on the bash strip).
- **In-chat hint** (`meridian:foreground-bash-hint`): once per foreground **`$`** task when the slot blocks — `/ps to manage tasks · ctrl+b to run in background`. Shown immediately (not `deliverAs: followUp`). Text in `details.hintText` (empty `content`); `triggerTurn: false`. Not for trailing **`&`** (already detached).
- **`/ps`**: **`b`** on foreground row → `backgroundForegroundBash` → `releaseWait` + clear foreground slot; exec exits `0` with `Sent to background — /ps`. Footer shows **`b` background** and **`ctrl+b`** when applicable.
- Agent **`bash`**: lifecycle via `tool_result` only (no `&` auto-background).
- **Status widget** (below editor, pi-processes style): live tasks only — `tasks: <name> running`; optional **`(ctrl+b …)`** while foreground `$` blocks. No ping countdown or session policy line (ping detail in **`/ps`** selection + **`/ps:settings`**).

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
