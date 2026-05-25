import { readFileSync } from "node:fs";

import type { MeridianEventBus } from "../../../shared/meridian_event_bus";
import {
  extractSpawnIdFromLauncherLog,
  isMeridianSpawnId,
  isSpawnLauncherCommand,
} from "../../../shared/meridian_spawn";
import type { TaskRegistry } from "../task_registry";
import {
  confirmSpawnRecord,
  fetchSpawnChildrenIds,
  isActiveSpawnStatus,
  type ConfirmedSpawnRecord,
} from "./spawn_record";

const TASK_END = "meridian:task:end";
const TASK_OUTPUT = "meridian:task:output";
const POLL_MS = 5_000;

type DiscoverOptions = {
  getRegistry: () => TaskRegistry | null;
  /** Host spawn for this Pi session (`MERIDIAN_SPAWN_ID`); scopes CLI discovery. */
  getOwnerSpawnId?: () => string | null;
};

export type SpawnCandidateMeta = {
  task_id?: string;
};

function readLogTail(path: string, maxBytes = 32_768): string {
  try {
    return readFileSync(path, "utf-8").slice(-maxBytes);
  } catch {
    return "";
  }
}

async function emitConfirmedSpawn(
  bus: MeridianEventBus,
  known: Map<string, ConfirmedSpawnRecord>,
  record: ConfirmedSpawnRecord,
  channel: "meridian:spawn:discovered" | "meridian:spawn:updated",
): Promise<void> {
  const prior = known.get(record.spawn_id);
  known.set(record.spawn_id, record);
  bus.emit(channel, {
    spawn_id: record.spawn_id,
    status: record.status,
    task_id: record.task_id,
    summary: record.summary,
  });
  if (
    prior &&
    !isActiveSpawnStatus(prior.status) &&
    !isActiveSpawnStatus(record.status)
  ) {
    return;
  }
}

async function confirmAndEmit(
  bus: MeridianEventBus,
  known: Map<string, ConfirmedSpawnRecord>,
  spawnId: string,
  taskIdHint?: string,
): Promise<void> {
  const record = await confirmSpawnRecord(spawnId);
  if (!record) {
    return;
  }
  const prior = known.get(spawnId);
  if (taskIdHint) {
    record.task_id = taskIdHint;
  } else if (prior?.task_id) {
    record.task_id = prior.task_id;
  }
  const channel =
    prior == null ? "meridian:spawn:discovered" : "meridian:spawn:updated";
  await emitConfirmedSpawn(bus, known, record, channel);
}

export async function collectSpawnCandidates(
  registry: TaskRegistry | null,
  ownerSpawnId: string | null,
): Promise<Map<string, SpawnCandidateMeta>> {
  const candidates = new Map<string, SpawnCandidateMeta>();

  if (ownerSpawnId && isMeridianSpawnId(ownerSpawnId)) {
    for (const id of await fetchSpawnChildrenIds(ownerSpawnId)) {
      if (!candidates.has(id)) {
        candidates.set(id, {});
      }
    }
  }

  if (registry) {
    for (const task of await registry.list(true)) {
      if (!task.combined_log_path || !isSpawnLauncherCommand(task.command)) {
        continue;
      }
      const spawnId = extractSpawnIdFromLauncherLog(readLogTail(task.combined_log_path));
      if (!spawnId) {
        continue;
      }
      candidates.set(spawnId, { task_id: task.task_id });
    }
  }

  return candidates;
}

/** @deprecated Use collectSpawnCandidates; returns id set only. */
export async function collectSpawnCandidateIds(
  registry: TaskRegistry | null,
  ownerSpawnId: string | null,
): Promise<Set<string>> {
  return new Set((await collectSpawnCandidates(registry, ownerSpawnId)).keys());
}

async function discoverFromRegistry(
  bus: MeridianEventBus,
  known: Map<string, ConfirmedSpawnRecord>,
  registry: TaskRegistry | null,
  ownerSpawnId: string | null,
): Promise<void> {
  const candidates = await collectSpawnCandidates(registry, ownerSpawnId);

  for (const [spawnId, meta] of candidates) {
    if (!isMeridianSpawnId(spawnId)) {
      continue;
    }
    await confirmAndEmit(bus, known, spawnId, meta.task_id);
  }
}

async function refreshActiveSpawns(
  bus: MeridianEventBus,
  known: Map<string, ConfirmedSpawnRecord>,
): Promise<void> {
  const activeIds = [...known.values()]
    .filter((row) => isActiveSpawnStatus(row.status))
    .map((row) => row.spawn_id);
  for (const spawnId of activeIds) {
    const prior = known.get(spawnId);
    await confirmAndEmit(bus, known, spawnId, prior?.task_id);
  }
}

export function startSpawnDiscovery(
  bus: MeridianEventBus,
  options: DiscoverOptions,
): () => void {
  const known = new Map<string, ConfirmedSpawnRecord>();
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let refreshInFlight = false;

  const runRefresh = async (): Promise<void> => {
    if (refreshInFlight) {
      return;
    }
    refreshInFlight = true;
    try {
      const ownerSpawnId = options.getOwnerSpawnId?.() ?? null;
      await discoverFromRegistry(bus, known, options.getRegistry(), ownerSpawnId);
      await refreshActiveSpawns(bus, known);
    } finally {
      refreshInFlight = false;
    }
  };

  void runRefresh();

  const onTaskActivity = (): void => {
    void runRefresh();
  };

  const unsubs = [
    bus.on(TASK_END, onTaskActivity),
    bus.on(TASK_OUTPUT, onTaskActivity),
  ];

  pollTimer = setInterval(() => {
    void runRefresh();
  }, POLL_MS);

  return () => {
    if (pollTimer != null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    for (const unsub of unsubs) {
      unsub();
    }
  };
}
