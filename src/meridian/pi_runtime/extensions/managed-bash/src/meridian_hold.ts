import { readdir } from "node:fs/promises";
import path from "node:path";
import { Value } from "typebox/value";
import { BashHoldSchema, ownerIsAlive } from "../../shared/delivery_receipts";
import { readPrivateJson } from "../../shared/json_file";
import { resolveBashHoldDir } from "../../shared/pi_state_paths";

/** Longest a held task stays in the foreground, measured from its start. */
export const MAX_HOLD_MIN = 55;
/** The variable that tells the CLI inside a task where to write its hold file. */
export const HOLD_DIR_ENV = "_MERIDIAN_PI_BASH_HOLD_DIR";

/**
 * True while a CLI process inside this task is running a `meridian spawn wait` or a foreground
 * `meridian spawn`: some file in the task's hold directory names an owner that is still alive.
 * A backgrounded wait would consume the child's report while the bash completion notice carries
 * no output. A missing directory, an unparseable file, or a dead owner means "not held"; a
 * crashed holder therefore needs no cleanup. `ps` runs only when a file exists.
 */
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
