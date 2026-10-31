import { readPrivateJson } from "../../shared/json_file";
import { resolveBashHoldPath } from "../../shared/pi_state_paths";

/** Longest a held task stays in the foreground, measured from its start. */
export const MAX_HOLD_MIN = 55;

/**
 * Whether a task's expiry re-arms instead of backgrounding. Only a task without an explicit
 * `timeout_min` is held, and only while it has been running less than `MAX_HOLD_MIN`.
 */
export function keepsForeground(held: boolean, explicitTimeout: boolean, elapsedMs: number): boolean {
  return held && !explicitTimeout && elapsedMs < MAX_HOLD_MIN * 60_000;
}

/**
 * True while the CLI running inside this task keeps its hold file renewed (a `meridian spawn
 * wait` or a foreground `meridian spawn`). A backgrounded wait would consume the child's report
 * while the bash completion notice carries no output. A missing, unreadable or expired file
 * means "not held".
 */
export async function holdsMeridianWait(sessionSpawnId: string, bashId: string): Promise<boolean> {
  const hold = await readPrivateJson(resolveBashHoldPath(bashId, sessionSpawnId)).catch(() => undefined);
  const expires = (hold as { expires_at_epoch?: unknown } | undefined)?.expires_at_epoch;
  return typeof expires === "number" && expires > Date.now() / 1000;
}
