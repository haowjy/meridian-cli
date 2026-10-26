import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { Type } from "typebox";
import { Value } from "typebox/value";
import { readPrivateJson, writeJsonAtomic } from "./json_file";
import {
  currentSpawnIdFromEnv,
  resolveDeliveryReceiptsPath,
  resolveLegacyObservedSpawnsPath, // legacy-observed-spawns (#719)
  resolveObservedSpawnsPath,
} from "./pi_state_paths";
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

export type WaitReservation = NonNullable<ObservedSpawnsFile["wait_reservations"]>[string];
const receiptQueues = new Map<string, Promise<void>>();

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

async function readObservationsFile(file: string, spawnId: string): Promise<ObservedSpawnsFile> {
  const value = await readPrivateJson(file);
  if (value === undefined) return { v: 1, spawn_id: spawnId, updated_at_ms: 0, observed_spawn_ids: [] };
  if (!Value.Check(ObservedSchema, value) || value.spawn_id !== spawnId) throw Error("invalid spawn observations");
  if (value.updated_at_ms != null && !Number.isFinite(value.updated_at_ms)) throw Error("invalid observation timestamp");
  for (const lease of Object.values(value.wait_reservations ?? {})) {
    if (![lease.owner_birth_epoch, lease.expires_at_epoch].every(Number.isFinite)) throw Error("invalid wait lease");
  }
  return { updated_at_ms: 0, ...value };
}

/**
 * legacy-observed-spawns (#719)
 * A `spawn wait` from a build before the move may still write the old file.
 * Compatibility reads do not repair it, so damaged content counts as absent;
 * an I/O failure (which carries an errno code) is still unknown. A later mirrored
 * record may replace the damaged copy.
 */
async function readLegacyObservations(spawnId: string): Promise<ObservedSpawnsFile> {
  try { return await readObservationsFile(resolveLegacyObservedSpawnsPath(spawnId), spawnId); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== undefined) throw error;
    return { v: 1, spawn_id: spawnId, updated_at_ms: 0, observed_spawn_ids: [] };
  }
}

/** legacy-observed-spawns (#719): Merge consumed ids and leases from both files. */
export async function readSpawnObservations(spawnId = currentSpawnIdFromEnv()): Promise<ObservedSpawnsFile> {
  const current = await readObservationsFile(resolveObservedSpawnsPath(spawnId), spawnId);
  const legacy = await readLegacyObservations(spawnId);
  const legacyLeases = Object.entries(legacy.wait_reservations ?? {}).map(([token, lease]) => [`legacy:${token}`, lease] as const);
  return {
    v: 1, spawn_id: spawnId,
    updated_at_ms: Math.max(current.updated_at_ms, legacy.updated_at_ms),
    observed_spawn_ids: [...current.observed_spawn_ids, ...legacy.observed_spawn_ids],
    wait_reservations: { ...Object.fromEntries(legacyLeases), ...current.wait_reservations },
  };
}

export async function reservedSpawnIds(leases: Iterable<WaitReservation>): Promise<Set<string>> {
  const result = new Set<string>();
  for (const lease of leases) {
    if (lease.expires_at_epoch <= Date.now() / 1000) continue;
    try {
      const { stdout } = await execFileAsync("ps", ["-o", "lstart=", "-p", String(lease.owner_pid)],
        { timeout: 1000, env: { ...process.env, LC_ALL: "C" } });
      const birth = Date.parse(stdout.trim()) / 1000;
      if (!Number.isFinite(birth)) throw Error("unreadable wait owner identity");
      if (Math.abs(birth - lease.owner_birth_epoch) > 2) continue;
    } catch (error) {
      if ((error as { code?: unknown }).code === 1) continue; // ps reports no matching PID
      throw error;
    }
    for (const id of lease.spawn_ids) result.add(id);
  }
  return result;
}
