import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { Type } from "typebox";
import { Value } from "typebox/value";
import { readPrivateJson, writeJsonAtomic } from "./json_file";
import {
  currentSpawnIdFromEnv,
  resolveDeliveryReceiptsPath,
  resolveLegacyDeliveryReceiptsPath,
  resolveLegacyObservedSpawnsPath,
  resolveObservedLaunchesPath,
} from "./pi_state_paths";
import { launchKey, type DeliveryReceiptsFile, type ObservedLaunchesFile } from "./schemas";

const execFileAsync = promisify(execFile);
const ReceiptsSchema = Type.Object({
  v: Type.Literal(1), spawn_id: Type.String(), messages: Type.Record(Type.String(), Type.Array(Type.String())),
});
const Lease = { owner_pid: Type.Integer({ minimum: 1 }), owner_birth_epoch: Type.Number({ exclusiveMinimum: 0 }),
  expires_at_epoch: Type.Number(), owner_bash_id: Type.Optional(Type.Union([Type.String(), Type.Null()])) };
const ObservedLaunchesSchema = Type.Object({
  v: Type.Literal(1), spawn_id: Type.String(), updated_at_ms: Type.Optional(Type.Number()),
  observed_launches: Type.Array(Type.String()),
  wait_reservations: Type.Optional(Type.Record(Type.String(), Type.Object({
    ...Lease, launches: Type.Array(Type.String()),
  }))),
});
// What older builds wrote in ``observed-spawns.json``, keyed by bare spawn ID.
const LegacyObservedSchema = Type.Object({
  v: Type.Literal(1), spawn_id: Type.String(), updated_at_ms: Type.Optional(Type.Number()),
  observed_spawn_ids: Type.Array(Type.String()),
  wait_reservations: Type.Optional(Type.Record(Type.String(), Type.Object({
    ...Lease, spawn_ids: Type.Array(Type.String()),
  }))),
});

export type WaitReservation = NonNullable<ObservedLaunchesFile["wait_reservations"]>[string];
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

function checkLeases(leases: Iterable<{ owner_birth_epoch: number; expires_at_epoch: number }>): void {
  for (const lease of leases) {
    if (![lease.owner_birth_epoch, lease.expires_at_epoch].every(Number.isFinite)) throw Error("invalid wait lease");
  }
}

/** A parent's current observations (``spawns/<parent>/observed-launches.json``). Damaged content is an error. */
export async function readCurrentObservations(spawnId = currentSpawnIdFromEnv()): Promise<ObservedLaunchesFile> {
  const value = await readPrivateJson(resolveObservedLaunchesPath(spawnId));
  if (value === undefined) return { v: 1, spawn_id: spawnId, updated_at_ms: 0, observed_launches: [] };
  if (!Value.Check(ObservedLaunchesSchema, value) || value.spawn_id !== spawnId) throw Error("invalid launch observations");
  if (value.updated_at_ms != null && !Number.isFinite(value.updated_at_ms)) throw Error("invalid observation timestamp");
  checkLeases(Object.values(value.wait_reservations ?? {}));
  return { updated_at_ms: 0, ...value };
}

/**
 * What older builds wrote (``spawns/<parent>/observed-spawns.json``), as launch-1 observations.
 * Never repaired, so damaged content counts as absent; an I/O failure (which carries an errno
 * code) is still unknown.
 */
export async function readLegacyObservations(spawnId: string): Promise<ObservedLaunchesFile> {
  const empty: ObservedLaunchesFile = { v: 1, spawn_id: spawnId, updated_at_ms: 0, observed_launches: [] };
  try {
    const value = await readPrivateJson(resolveLegacyObservedSpawnsPath(spawnId));
    if (value === undefined) return empty;
    if (!Value.Check(LegacyObservedSchema, value) || value.spawn_id !== spawnId) throw Error("invalid spawn observations");
    const leases = Object.entries(value.wait_reservations ?? {});
    checkLeases(leases.map(([, lease]) => lease));
    return {
      ...empty,
      observed_launches: value.observed_spawn_ids.map((id) => launchKey(id, 1)),
      wait_reservations: Object.fromEntries(leases.map(([token, { spawn_ids, ...lease }]) =>
        [token, { ...lease, launches: spawn_ids.map((id) => launchKey(id, 1)) }])),
    };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== undefined) throw error;
    return empty;
  }
}

/** Older builds' receipts (``delivery-receipts.json``): bare IDs, read-only. */
export async function readLegacyDeliveryReceipts(spawnId: string): Promise<DeliveryReceiptsFile> {
  return readDeliveryReceipts(spawnId, resolveLegacyDeliveryReceiptsPath(spawnId));
}

export async function reservedLaunchKeys(leases: Iterable<WaitReservation>): Promise<Set<string>> {
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
    for (const key of lease.launches) result.add(key);
  }
  return result;
}
