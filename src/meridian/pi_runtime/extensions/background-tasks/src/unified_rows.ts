import type { MeridianEventBus } from "../../shared/meridian_event_bus";
import type { BackgroundTaskRecord, PsRow } from "./types";

const SPAWN_CHANNELS = [
  "meridian:spawn:discovered",
  "meridian:spawn:updated",
  "meridian:spawn:removed",
] as const;

export type SpawnRowProjection = {
  spawn_id: string;
  task_id?: string;
  status: string;
  summary?: string;
};

export function createUnifiedRowFeed(bus: MeridianEventBus): {
  getSpawnRows: () => SpawnRowProjection[];
  mergeRows: (tasks: BackgroundTaskRecord[]) => PsRow[];
  dispose: () => void;
} {
  const spawnRows = new Map<string, SpawnRowProjection>();

  const unsubs = SPAWN_CHANNELS.map((channel) =>
    bus.on(channel, (payload) => {
      const spawnId = typeof payload.spawn_id === "string" ? payload.spawn_id : null;
      if (!spawnId) {
        return;
      }
      if (channel === "meridian:spawn:removed") {
        spawnRows.delete(spawnId);
        return;
      }
      spawnRows.set(spawnId, {
        spawn_id: spawnId,
        task_id: typeof payload.task_id === "string" ? payload.task_id : undefined,
        status: typeof payload.status === "string" ? payload.status : "unknown",
        summary: typeof payload.summary === "string" ? payload.summary : undefined,
      });
    }),
  );

  return {
    getSpawnRows: () => [...spawnRows.values()],
    mergeRows(tasks) {
      const processRows: PsRow[] = tasks.map((task) => ({
        kind: "process" as const,
        ...task,
      }));
      const spawnPsRows: PsRow[] = [...spawnRows.values()].map((row) => ({
        kind: "meridian_spawn",
        spawn_id: row.spawn_id,
        task_id: row.task_id,
        status: row.status,
        summary: row.summary,
      }));
      return [...processRows, ...spawnPsRows];
    },
    dispose() {
      for (const unsub of unsubs) {
        unsub();
      }
    },
  };
}
