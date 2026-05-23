# extensions/background-tasks/

Generic OS background task registry, `background_task` tool, unified `/ps*` UI (pi-processes parity).

State: `{MERIDIAN_PI_STATE_DIR}/background-tasks/{sessionId}/tasks/{task_id}/`.

Consumes `meridian:spawn:*` on Pi's shared `pi.events` bus (via `resolveExtensionBus`) for unified `/ps` rows.

## UX reference

Clone and refresh before parity work:

```bash
# already at ~/gitrepos/ref/pi-processes — git pull --ff-only
```

Map: work dir `pi-processes-parity-map.md` (meridian-cli work item). Pair with `meridian-spawn-watch` for `/spawns` tree.

## Interactive `$` bash

- Pi **`!`** runs a command shortcut — not used for background.
- User **`$`** with a trailing shell **`&`** (e.g. `$ sleep 1000 &`) detaches immediately via `bash_bridge` `result` (frees `isBashRunning`); task stays in TaskRegistry and `/ps`.
- Foreground **`$`** runs block the prompt until exit; `/ps` pins that row at the top with **`● fg`**, and **`b`** calls `releaseWait` without killing the process.
- Agent **`bash`** tool calls are unchanged (no auto-background on `&`).

## Verify after each implementation pass

1. `cd src/meridian/pi_runtime && npm run verify:extensions` (build + vitest + bundle smoke).
2. Delegate **smoke-tester** (verify mode) for `meridian pi` / RPC claims — mandatory for keyboard and spawn integration.
3. Diff touched UI/commands against the same path under `ref/pi-processes/src/`.
