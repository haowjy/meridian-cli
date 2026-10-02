import { readFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { Type } from "typebox";
import { Value } from "typebox/value";
import { writeJsonAtomic } from "./json_file";
import { currentSpawnIdFromEnv, resolveDeliveryReceiptsPath, resolveObservedSpawnsPath } from "./pi_state_paths";
import type { DeliveryReceiptsFile, ObservedSpawnsFile } from "./schemas";

const execFileAsync = promisify(execFile);
const ReceiptsSchema = Type.Object({
  v: Type.Literal(1), spawn_id: Type.String(), messages: Type.Record(Type.String(), Type.Array(Type.String())),
});
const ObservedSchema = Type.Object({
  v: Type.Literal(1), spawn_id: Type.String(), updated_at_ms: Type.Optional(Type.Number()),
  observed_spawn_ids: Type.Array(Type.String()), waiting_spawn_ids: Type.Optional(Type.Array(Type.String())),
  wait_reservations: Type.Optional(Type.Record(Type.String(), Type.Object({
    owner_pid: Type.Integer({ minimum: 1 }), owner_birth_epoch: Type.Number({ exclusiveMinimum: 0 }),
    expires_at_epoch: Type.Number(), spawn_ids: Type.Array(Type.String()),
  }))),
});
const receiptQueues = new Map<string, Promise<void>>();

/** Invalid present bytes never become a successful empty read. */
export async function readPrivateJson(file: string): Promise<unknown | undefined> {
  try { return JSON.parse(await readFile(file, "utf8")) as unknown; }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
}

export async function readDeliveryReceipts(spawnId = currentSpawnIdFromEnv(), file = resolveDeliveryReceiptsPath(spawnId)): Promise<DeliveryReceiptsFile> {
  const value = await readPrivateJson(file);
  if (value === undefined) return { v: 1, spawn_id: spawnId, messages: {} };
  if (!Value.Check(ReceiptsSchema, value) || value.spawn_id !== spawnId) throw Error("invalid delivery receipts");
  return value;
}

export async function admitDelivery(deliveryId: string, workIds: string[], spawnId = currentSpawnIdFromEnv(), file = resolveDeliveryReceiptsPath(spawnId)): Promise<void> {
  const next = (receiptQueues.get(file) ?? Promise.resolve()).catch(() => undefined).then(async () => {
    const value = await readDeliveryReceipts(spawnId, file);
    const prior = value.messages[deliveryId];
    if (prior && JSON.stringify(prior) !== JSON.stringify(workIds)) throw Error("delivery identity changed");
    value.messages[deliveryId] = workIds;
    await writeJsonAtomic(file, value);
  });
  receiptQueues.set(file, next);
  await next;
}

export function admittedWorkIds(receipts: DeliveryReceiptsFile): Set<string> {
  return new Set(Object.values(receipts.messages).flat());
}

export async function readSpawnObservations(spawnId = currentSpawnIdFromEnv(), file = resolveObservedSpawnsPath(spawnId)): Promise<ObservedSpawnsFile> {
  const value = await readPrivateJson(file);
  if (value === undefined) return { v: 1, spawn_id: spawnId, updated_at_ms: 0, observed_spawn_ids: [] };
  if (!Value.Check(ObservedSchema, value) || value.spawn_id !== spawnId) throw Error("invalid spawn observations");
  if (value.updated_at_ms != null && !Number.isFinite(value.updated_at_ms)) throw Error("invalid observation timestamp");
  for (const lease of Object.values(value.wait_reservations ?? {})) {
    if (![lease.owner_birth_epoch, lease.expires_at_epoch].every(Number.isFinite)) throw Error("invalid wait lease");
  }
  return { updated_at_ms: 0, ...value };
}

export async function reservedSpawnIds(observed: ObservedSpawnsFile): Promise<Set<string>> {
  const result = new Set<string>();
  for (const lease of Object.values(observed.wait_reservations ?? {})) {
    if (lease.expires_at_epoch <= Date.now() / 1000) continue;
    try {
      const { stdout } = await execFileAsync("ps", ["-o", "lstart=", "-p", String(lease.owner_pid)],
        { timeout: 1000, env: { ...process.env, LC_ALL: "C" } });
      const birth = Date.parse(stdout.trim()) / 1000;
      if (!Number.isFinite(birth) || Math.abs(birth - lease.owner_birth_epoch) > 2) continue;
    } catch { continue; }
    for (const id of lease.spawn_ids) result.add(id);
  }
  return result;
}
