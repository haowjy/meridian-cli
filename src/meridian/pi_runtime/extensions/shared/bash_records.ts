import { readPrivateJson } from "./json_file";
import { currentSpawnIdFromEnv, resolveBashRecordsPath } from "./pi_state_paths";
import { type BashRecordsFile, parseBashRecordsFile } from "./schemas";

/** Read one parent's authoritative managed-bash snapshot without weakening bad evidence to empty. */
export async function readBashRecords(
  spawnId = currentSpawnIdFromEnv(),
  filePath = resolveBashRecordsPath(spawnId),
): Promise<BashRecordsFile> {
  const value = await readPrivateJson(filePath);
  if (value === undefined) {
    return { v: 1, spawn_id: spawnId, updated_at_ms: 0, records: {} };
  }
  const file = parseBashRecordsFile(value);
  if (!file || file.spawn_id !== spawnId || file.runtime_error) {
    throw new Error("invalid or unresolved bash evidence");
  }
  return file;
}
