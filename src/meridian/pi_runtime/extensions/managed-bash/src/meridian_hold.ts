import { readdir } from "node:fs/promises";
import path from "node:path";
import { Value } from "typebox/value";
import { BashHoldSchema, ownerIsAlive } from "../../shared/process_identity";
import { readPrivateJson } from "../../shared/json_file";
import { resolveBashHoldDir } from "../../shared/pi_state_paths";

export const MAX_HOLD_MIN = 55;
export const HOLD_DIR_ENV = "_MERIDIAN_PI_BASH_HOLD_DIR";

/** A backgrounded wait can consume its report while the bash completion notice carries no output. */
export async function taskHoldsMeridianCommand(sessionSpawnId: string, bashId: string): Promise<boolean> {
  const dir = resolveBashHoldDir(bashId, sessionSpawnId);
  const names = await readdir(dir).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return [];
    throw error;
  });
  for (const name of names) {
    if (!name.endsWith(".json")) continue; // skips an atomic write's temp file
    const hold = await readPrivateJson(path.join(dir, name)).catch(() => undefined);
    if (Value.Check(BashHoldSchema, hold) && await ownerIsAlive(hold)) return true;
  }
  return false;
}
