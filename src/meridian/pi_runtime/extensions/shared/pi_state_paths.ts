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

export function resolvePiBashDir(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolveStateRoot(), "pi-bash", spawnId);
}

export function resolveBashRecordsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "bash-records.json");
}

export function resolveLastNotificationPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "last-notification.json");
}

export function resolveObservedSpawnsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "observed-spawns.json");
}

export function resolveClearedSpawnsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "cleared-spawns.json");
}

export function resolveSpawnOriginsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "spawn-origins.json");
}

export function resolveBashLogsDir(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "logs");
}

export function resolveSpawnsDir(): string {
  return path.join(resolveStateRoot(), "spawns");
}
