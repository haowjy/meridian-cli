import { readdir, stat } from "node:fs/promises";
import path from "node:path";
import { readCurrentObservations } from "../../shared/delivery_receipts";
import { readPrivateJson } from "../../shared/json_file";
import { resolveSpawnsDir } from "../../shared/pi_state_paths";
import { isBackgroundLaunch, parseSpawnStateFile } from "../../shared/schemas";

/**
 * True while a task is running a Meridian wait or a foreground spawn, so its 4-minute timer
 * re-arms instead of backgrounding. A backgrounded wait would consume the child's report while
 * the bash completion notice carries no output. Any unreadable evidence means "not held".
 *
 * - a wait renews a lease in this session's `observed-launches.json`, tagged with its task id;
 * - a foreground spawn is a running, non-background child row of this session with the task id
 *   as `originating_bash_id`. A task's children are created after the task started, so a row
 *   file untouched since then cannot be one and is skipped without being read.
 */
export async function holdsMeridianWait(sessionSpawnId: string, bashId: string, startedAtMs: number): Promise<boolean> {
  try {
    const observed = await readCurrentObservations(sessionSpawnId);
    const now = Date.now() / 1000;
    if (Object.values(observed.wait_reservations ?? {}).some(
      (lease) => lease.owner_bash_id === bashId && lease.expires_at_epoch > now)) return true;
    const spawnsDir = resolveSpawnsDir();
    for (const name of await readdir(spawnsDir)) {
      if (!name.startsWith("p")) continue;
      const file = path.join(spawnsDir, name, "state.json");
      const info = await stat(file).catch(() => undefined);
      if (!info || info.mtimeMs < startedAtMs - 1000) continue;
      const row = parseSpawnStateFile(await readPrivateJson(file).catch(() => undefined));
      if (row && row.parent_id === sessionSpawnId && row.originating_bash_id === bashId
          && row.terminal === null && !isBackgroundLaunch(row)) return true;
    }
  } catch {
    // Damaged or missing evidence: treat as no wait, so the task backgrounds as usual.
  }
  return false;
}
