import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { Type } from "typebox";

const execFileAsync = promisify(execFile);

/** Schema fields naming a process by PID and birth time, so a reused PID is not mistaken for it. */
export const OwnerIdentity = {
  owner_pid: Type.Integer({ minimum: 1 }),
  owner_birth_epoch: Type.Number({ exclusiveMinimum: 0 }),
};

/** A hold file: the CLI process running a wait or foreground spawn inside one bash task. */
export const BashHoldSchema = Type.Object({ v: Type.Literal(1), ...OwnerIdentity });

/** Whether the process named by an owner identity is still the one that wrote it (PID and birth time). */
export async function ownerIsAlive(owner: { owner_pid: number; owner_birth_epoch: number }): Promise<boolean> {
  try {
    const { stdout } = await execFileAsync("ps", ["-o", "lstart=", "-p", String(owner.owner_pid)],
      { timeout: 1000, env: { ...process.env, LC_ALL: "C" } });
    const birth = Date.parse(stdout.trim()) / 1000;
    if (!Number.isFinite(birth)) throw Error("unreadable process birth time");
    return Math.abs(birth - owner.owner_birth_epoch) <= 2;
  } catch (error) {
    if ((error as { code?: unknown }).code === 1) return false; // ps reports no matching PID
    throw error;
  }
}
