import { emitMeridianEvent, offMeridianEvent, onMeridianEvent } from "../../shared/meridian_bus";
import type { SpawnTreeStore } from "./tree";

const TASK_START = "meridian:task:start";
const TASK_END = "meridian:task:end";
const SUBSPAWN_START = "meridian:subspawn:start";
const SUBSPAWN_END = "meridian:subspawn:end";

export function startSpawnCollector(tree: SpawnTreeStore): () => void {
  const onTaskStart = async (payload: Record<string, unknown>) => {
    const spawnId = typeof payload.spawn_id === "string" ? payload.spawn_id : null;
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
    emitMeridianEvent("meridian:spawn:discovered", { spawn_id: spawnId, ...payload });
  };

  const onTaskEnd = async (payload: Record<string, unknown>) => {
    const spawnId = typeof payload.spawn_id === "string" ? payload.spawn_id : null;
    if (!spawnId) {
      return;
    }
    const file = await tree.read();
    const node = file.nodes.find((n) => n.spawn_id === spawnId);
    if (node) {
      node.status = typeof payload.status === "string" ? payload.status : "ended";
      await tree.write(file);
    }
    emitMeridianEvent("meridian:spawn:updated", { spawn_id: spawnId, ...payload });
  };

  onMeridianEvent(TASK_START, onTaskStart);
  onMeridianEvent(TASK_END, onTaskEnd);
  onMeridianEvent(SUBSPAWN_START, onTaskStart);
  onMeridianEvent(SUBSPAWN_END, onTaskEnd);

  return () => {
    offMeridianEvent(TASK_START, onTaskStart);
    offMeridianEvent(TASK_END, onTaskEnd);
    offMeridianEvent(SUBSPAWN_START, onTaskStart);
    offMeridianEvent(SUBSPAWN_END, onTaskEnd);
  };
}
