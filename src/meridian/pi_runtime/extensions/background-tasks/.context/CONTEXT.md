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

## Verify after each implementation pass

1. `cd src/meridian/pi_runtime && npm run verify:extensions` (build + vitest + bundle smoke).
2. Delegate **smoke-tester** (verify mode) for `meridian pi` / RPC claims — mandatory for keyboard and spawn integration.
3. Diff touched UI/commands against the same path under `ref/pi-processes/src/`.
