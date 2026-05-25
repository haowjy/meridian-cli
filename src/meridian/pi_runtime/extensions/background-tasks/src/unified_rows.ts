import type { MeridianEventBus } from "../../shared/meridian_event_bus";
import type { BackgroundTaskRecord, MeridianSpawnAttachment, PsRow } from "./types";

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

function toAttachment(row: SpawnRowProjection): MeridianSpawnAttachment {
  return {
    spawn_id: row.spawn_id,
    status: row.status,
    summary: row.summary,
  };
}

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
      const byTaskId = new Map<string, SpawnRowProjection>();
      const unattached: SpawnRowProjection[] = [];

      for (const spawn of spawnRows.values()) {
        if (spawn.task_id) {
          byTaskId.set(spawn.task_id, spawn);
        } else {
          unattached.push(spawn);
        }
      }

      const taskIds = new Set(tasks.map((task) => task.task_id));
      const attachedSpawnIds = new Set<string>();

      const processRows: PsRow[] = tasks.map((task) => {
        const spawn = byTaskId.get(task.task_id);
        if (spawn) {
          attachedSpawnIds.add(spawn.spawn_id);
        }
        return {
          kind: "process" as const,
          ...task,
          meridian_spawn: spawn ? toAttachment(spawn) : undefined,
        };
      });

      const orphanSpawns = [
        ...unattached,
        ...[...spawnRows.values()].filter(
          (spawn) =>
            spawn.task_id != null &&
            !taskIds.has(spawn.task_id) &&
            !attachedSpawnIds.has(spawn.spawn_id),
        ),
      ];

      const orphanPsRows: PsRow[] = orphanSpawns.map((row) => ({
        kind: "meridian_spawn",
        spawn_id: row.spawn_id,
        task_id: row.task_id,
        status: row.status,
        summary: row.summary,
      }));

      return [...processRows, ...orphanPsRows];
    },
    dispose() {
      for (const unsub of unsubs) {
        unsub();
      }
    },
  };
}
