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

// Consumption ledgers are keyed by launch ("<spawn id>:<n>", see ``launchKey``). The files below
// named "legacy" are what older builds wrote, keyed by bare spawn ID: read-only, and an entry
// there names launch 1 (an older build cannot read a v4 row, so it only consumed launch 1).

export function resolveDeliveryReceiptsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "launch-receipts.json");
}

export function resolveLegacyDeliveryReceiptsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "delivery-receipts.json");
}

/** Mirrors ``paths.observed_launches_path``: the parent's own spawn directory. */
export function resolveObservedLaunchesPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolveSpawnsDir(), spawnId, "observed-launches.json");
}

/** Mirrors ``paths.observed_spawns_path``: the file older builds kept in the same place. */
export function resolveLegacyObservedSpawnsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolveSpawnsDir(), spawnId, "observed-spawns.json");
}

/**
 * Mirrors ``launch_archive``: launch ``n``'s finished row, copied by the continue that started
 * launch ``n+1``. A launch the spawn has moved past is read from here, never from ``state.json``.
 */
export function resolveLaunchArchivePath(spawnId: string, n: number): string {
  return path.join(resolveSpawnsDir(), spawnId, "launches", String(n), "launch.json");
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

export function resolveClearedLaunchesPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "cleared-launches.json");
}

export function resolveLegacyClearedSpawnsPath(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "cleared-spawns.json");
}

export function resolveBashLogsDir(spawnId = currentSpawnIdFromEnv()): string {
  return path.join(resolvePiBashDir(spawnId), "logs");
}

export function resolveSpawnsDir(): string {
  return path.join(resolveStateRoot(), "spawns");
}
