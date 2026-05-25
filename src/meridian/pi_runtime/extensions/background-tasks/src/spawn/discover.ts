import { readFileSync } from "node:fs";

import type { MeridianEventBus } from "../../../shared/meridian_event_bus";
import { extractMeridianSpawnIdsFromText, isMeridianSpawnId } from "../../../shared/meridian_spawn";
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
): Promise<void> {
  const record = await confirmSpawnRecord(spawnId);
  if (!record) {
    return;
  }
  const prior = known.get(spawnId);
  const channel =
    prior == null ? "meridian:spawn:discovered" : "meridian:spawn:updated";
  await emitConfirmedSpawn(bus, known, record, channel);
}

export async function collectSpawnCandidateIds(
  registry: TaskRegistry | null,
  ownerSpawnId: string | null,
): Promise<Set<string>> {
  const candidateIds = new Set<string>();

  if (ownerSpawnId && isMeridianSpawnId(ownerSpawnId)) {
    for (const id of await fetchSpawnChildrenIds(ownerSpawnId)) {
      candidateIds.add(id);
    }
  }

  if (registry) {
    for (const task of await registry.list(true)) {
      if (!task.combined_log_path) {
        continue;
      }
      const tail = readLogTail(task.combined_log_path);
      for (const id of extractMeridianSpawnIdsFromText(tail)) {
        candidateIds.add(id);
      }
    }
  }

  return candidateIds;
}

async function discoverFromRegistry(
  bus: MeridianEventBus,
  known: Map<string, ConfirmedSpawnRecord>,
  registry: TaskRegistry | null,
  ownerSpawnId: string | null,
): Promise<void> {
  const candidateIds = await collectSpawnCandidateIds(registry, ownerSpawnId);

  for (const spawnId of candidateIds) {
    if (!isMeridianSpawnId(spawnId)) {
      continue;
    }
    await confirmAndEmit(bus, known, spawnId);
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
    await confirmAndEmit(bus, known, spawnId);
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
