import type { MeridianEventBus } from "../../../shared/meridian_event_bus";
import { isMeridianSpawnId } from "../../../shared/meridian_spawn";

const TASK_START = "meridian:task:start";
const TASK_END = "meridian:task:end";
const SUBSPAWN_START = "meridian:subspawn:start";
const SUBSPAWN_END = "meridian:subspawn:end";

function resolveSpawnId(payload: Record<string, unknown>): string | null {
  if (typeof payload.spawn_id === "string" && isMeridianSpawnId(payload.spawn_id)) {
    return payload.spawn_id;
  }
  const kind = payload.kind;
  if (kind === "meridian_spawn" && typeof payload.subspawn_id === "string") {
    const subspawnId = payload.subspawn_id;
    if (isMeridianSpawnId(subspawnId)) {
      return subspawnId;
    }
  }
  return null;
}

/** Forward subspawn/task envelopes that already carry a confirmed spawn id. */
export function startSpawnCollector(bus: MeridianEventBus): () => void {
  const onDiscovered = (payload: Record<string, unknown>): void => {
    const spawnId = resolveSpawnId(payload);
    if (!spawnId) {
      return;
    }
    bus.emit("meridian:spawn:discovered", {
      spawn_id: spawnId,
      status: typeof payload.status === "string" ? payload.status : "discovered",
      task_id: typeof payload.task_id === "string" ? payload.task_id : undefined,
      summary: typeof payload.label === "string" ? payload.label : undefined,
      ...payload,
    });
  };

  const onUpdated = (payload: Record<string, unknown>): void => {
    const spawnId = resolveSpawnId(payload);
    if (!spawnId) {
      return;
    }
    bus.emit("meridian:spawn:updated", {
      spawn_id: spawnId,
      status: typeof payload.status === "string" ? payload.status : "ended",
      task_id: typeof payload.task_id === "string" ? payload.task_id : undefined,
      ...payload,
    });
  };

  const unsubs = [
    bus.on(TASK_START, onDiscovered),
    bus.on(SUBSPAWN_START, onDiscovered),
    bus.on(TASK_END, onUpdated),
    bus.on(SUBSPAWN_END, onUpdated),
  ];

  return () => {
    for (const unsub of unsubs) {
      unsub();
    }
  };
}
