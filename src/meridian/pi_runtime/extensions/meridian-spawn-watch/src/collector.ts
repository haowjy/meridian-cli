import type { MeridianEventBus } from "../../shared/meridian_event_bus";
import type { SpawnTreeStore } from "./tree";

const TASK_START = "meridian:task:start";
const TASK_END = "meridian:task:end";
const SUBSPAWN_START = "meridian:subspawn:start";
const SUBSPAWN_END = "meridian:subspawn:end";

function resolveSpawnId(payload: Record<string, unknown>): string | null {
  if (typeof payload.spawn_id === "string" && payload.spawn_id.trim().length > 0) {
    return payload.spawn_id;
  }
  const kind = payload.kind;
  if (kind === "meridian_spawn" && typeof payload.subspawn_id === "string") {
    return payload.subspawn_id;
  }
  return null;
}

export function startSpawnCollector(tree: SpawnTreeStore, bus: MeridianEventBus): () => void {
  const onDiscovered = async (payload: Record<string, unknown>) => {
    const spawnId = resolveSpawnId(payload);
    if (!spawnId) {
      return;
    }
    const file = await tree.read();
    if (file.nodes.some((n) => n.spawn_id === spawnId)) {
      return;
    }
    file.nodes.push({
      spawn_id: spawnId,
      parent_spawn_id:
        typeof payload.parent_spawn_id === "string" ? payload.parent_spawn_id : undefined,
      task_id: typeof payload.task_id === "string" ? payload.task_id : undefined,
      kind:
        payload.kind === "meridian_spawn_wrapper" ? "meridian_spawn_wrapper" : "meridian_spawn",
      status: typeof payload.status === "string" ? payload.status : "discovered",
      label: typeof payload.label === "string" ? payload.label : undefined,
      discovered_at_ms: Date.now(),
    });
    await tree.write(file);
    bus.emit("meridian:spawn:discovered", { spawn_id: spawnId, ...payload });
  };

  const onUpdated = async (payload: Record<string, unknown>) => {
    const spawnId = resolveSpawnId(payload);
    if (!spawnId) {
      return;
    }
    const file = await tree.read();
    const node = file.nodes.find((n) => n.spawn_id === spawnId);
    if (node) {
      node.status = typeof payload.status === "string" ? payload.status : "ended";
      await tree.write(file);
    }
    bus.emit("meridian:spawn:updated", { spawn_id: spawnId, ...payload });
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
