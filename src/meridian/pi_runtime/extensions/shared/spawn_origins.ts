import { readJsonFile, writeJsonAtomic } from "./json_file";
import { currentSpawnIdFromEnv, resolveSpawnOriginsPath } from "./pi_state_paths";

type TrackedSpawnOriginsFile = {
  v: 1;
  spawn_id: string;
  updated_at_ms: number;
  origin_bash_ids: string[];
};

const WRITE_QUEUES = new Map<string, Promise<void>>();

export async function readSpawnOriginBashIds(spawnId = currentSpawnIdFromEnv()): Promise<Set<string>> {
  const file = await readJsonFile<TrackedSpawnOriginsFile | null>(resolveSpawnOriginsPath(spawnId), null);
  return new Set((file?.origin_bash_ids ?? []).filter((id): id is string => typeof id === "string"));
}

export async function rememberSpawnOriginBashIds(
  ids: Iterable<string>,
  spawnId = currentSpawnIdFromEnv(),
): Promise<Set<string>> {
  const filePath = resolveSpawnOriginsPath(spawnId);
  let result = new Set<string>();
  const previous = WRITE_QUEUES.get(filePath) ?? Promise.resolve();
  const next = previous.catch(() => undefined).then(async () => {
    const originBashIds = await readSpawnOriginBashIds(spawnId);
    let changed = false;
    for (const id of ids) {
      if (id.length === 0 || originBashIds.has(id)) continue;
      originBashIds.add(id);
      changed = true;
    }
    if (changed) {
      await writeJsonAtomic(filePath, {
        v: 1,
        spawn_id: spawnId,
        updated_at_ms: Date.now(),
        origin_bash_ids: [...originBashIds].sort(),
      });
    }
    result = originBashIds;
  });
  WRITE_QUEUES.set(filePath, next.then(() => undefined, () => undefined));
  await next;
  return result;
}
