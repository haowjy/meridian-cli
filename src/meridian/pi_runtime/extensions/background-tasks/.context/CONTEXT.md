# extensions/background-tasks/

Generic OS background task registry, `background_task` tool, unified `/ps*` UI (pi-processes parity).

State: `{MERIDIAN_PI_STATE_DIR}/background-tasks/{sessionId}/tasks/{task_id}/`.

Consumes `meridian:spawn:*` on Pi's shared `pi.events` bus (via `resolveExtensionBus`) for unified `/ps` rows.
