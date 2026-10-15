import os from "node:os";
import path from "node:path";

/** Mirrors ``pi_paths.resolve_meridian_pi_state_dir`` — extension runtime state root. */
export function resolveStateRoot(): string {
  const explicit = process.env._MERIDIAN_PI_STATE_DIR?.trim();
  if (explicit) {
    return explicit;
  }
  const home = process.env.HOME?.trim();
  if (home) {
    return path.join(home, ".meridian", "meridian-pi", "state");
  }
  return path.join(os.homedir(), ".meridian", "meridian-pi", "state");
}

export function currentSpawnIdFromEnv(): string {
  return process.env.MERIDIAN_SPAWN_ID?.trim() || "unknown";
}

export function currentRunIdFromEnv(): string {
  return process.env.MERIDIAN_RUN_ID?.trim() || currentSpawnIdFromEnv();
}

export function resolveSpawnScopeDir(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolveStateRoot(), "pi-bash", spawnId);
}

export function resolveRunScopeDir(runId = currentRunIdFromEnv()): string {
  return path.join(resolveStateRoot(), "pi-bash", runId);
}

export function resolveBashRecordsPath(runId = currentRunIdFromEnv()): string {
  return path.join(resolveRunScopeDir(runId), "bash-records.json");
}

export function resolveObservedSpawnsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolveSpawnScopeDir(spawnId), "observed-spawns.json");
}

export function resolveClearedSpawnsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolveSpawnScopeDir(spawnId), "cleared-spawns.json");
}

export function resolveDeliveryReceiptsPath(runId = currentRunIdFromEnv()): string {
  return path.join(resolveRunScopeDir(runId), "delivery-receipts.json");
}

export function resolveDeliveryFaultPath(runId = currentRunIdFromEnv()): string {
  return path.join(resolveRunScopeDir(runId), "delivery-fault.json");
}

export function resolveBashLogsDir(runId = currentRunIdFromEnv()): string {
  return path.join(resolveRunScopeDir(runId), "logs");
}

export function resolveSpawnsDir(): string {
  return path.join(resolveStateRoot(), "spawns");
}
