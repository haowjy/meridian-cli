import path from "node:path";

/** Mirrors ``pi_paths.resolve_meridian_pi_state_dir`` — extension runtime state root. */
export function resolveStateRoot(): string {
  const runtimeRoot = process.env.MERIDIAN_RUNTIME_DIR?.trim();
  if (runtimeRoot) {
    return runtimeRoot;
  }
  const explicit = process.env.MERIDIAN_PI_STATE_DIR?.trim();
  if (explicit) {
    return explicit;
  }
  const sessionDir = process.env.PI_CODING_AGENT_SESSION_DIR?.trim();
  if (sessionDir) {
    return sessionDir;
  }
  const agentDir = process.env.PI_CODING_AGENT_DIR?.trim();
  if (agentDir) {
    return path.join(agentDir, ".meridian");
  }
  const home = process.env.HOME?.trim();
  if (home) {
    return path.join(home, ".meridian", "meridian-pi", "state");
  }
  return path.join(process.cwd(), ".meridian");
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

export function resolveBashLogsDir(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "logs");
}

export function resolveSpawnsDir(): string {
  return path.join(resolveStateRoot(), "spawns");
}
