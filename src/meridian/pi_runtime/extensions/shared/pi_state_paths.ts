import os from "node:os";
import path from "node:path";
import type { SpawnStateFile } from "./schemas";

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

/**
 * Parents whose direct children this session delivers. Core resolves the scope
 * for an interactive primary (every primary generation of its chat) and omits it
 * for a spawned or nested session, which owns only its own children.
 */
export function owedParentIdsFromEnv(spawnId = currentSpawnIdFromEnv()): ReadonlySet<string> {
  const raw = process.env._MERIDIAN_PI_OWED_PARENT_IDS?.trim();
  const ids = (raw ?? "").split(",").map((id) => id.trim()).filter(Boolean);
  return new Set(ids.length ? ids : [spawnId]);
}

export function resolvePiBashDir(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolveStateRoot(), "pi-bash", spawnId);
}

export function resolveBashRecordsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "bash-records.json");
}

export function resolveDeliveryReceiptsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "delivery-receipts.json");
}

/** Mirrors ``paths.observed_spawns_path``: the parent's own spawn directory. */
export function resolveObservedSpawnsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolveSpawnsDir(), spawnId, "observed-spawns.json");
}

/**
 * Mirrors ``LaunchPaths.report``: the one place the extension names a per-launch file.
 * Takes the parsed spawn state so the launch it names is the one resolved: a v4 row's
 * launch lives in ``launches/<n>/``, a v3 row's (``launch`` absent) in the spawn directory.
 */
export function resolveReportPath(row: SpawnStateFile): string {
  const launchDir = row.launch === undefined ? [] : ["launches", String(row.launch.n)];
  return path.join(resolveSpawnsDir(), row.id, ...launchDir, "report.md");
}

/** legacy-observed-spawns (#719): Old location, read-only in the Pi extension. */
export function resolveLegacyObservedSpawnsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "observed-spawns.json");
}

export function resolveClearedSpawnsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "cleared-spawns.json");
}

export function resolveBashLogsDir(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "logs");
}

export function resolveSpawnsDir(): string {
  return path.join(resolveStateRoot(), "spawns");
}
