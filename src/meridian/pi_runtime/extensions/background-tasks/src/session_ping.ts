export const DEFAULT_TASK_PING_INTERVAL_MS = 55 * 60 * 1000;
export const TASK_PING_INTERVAL_MS_ENV = "MERIDIAN_PI_TASK_PING_INTERVAL_MS";
export const TASK_PING_RESET_ON_ACTIVITY_ENV = "MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY";
export const TASK_PING_DEFAULT_PERSISTENT_ENV = "MERIDIAN_PI_TASK_PING_DEFAULT_PERSISTENT";

export type SpawnTaskPingDefaults = {
  pingIntervalMs: number | null;
  pingResetOnActivity: boolean;
  defaultPersistent: boolean;
};

function parseEnvBoolean(raw: string | undefined, fallback: boolean): boolean {
  if (raw == null || raw.trim().length === 0) {
    return fallback;
  }
  const normalized = raw.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off"].includes(normalized)) {
    return false;
  }
  return fallback;
}

function parseEnvPositiveMs(raw: string | undefined): number | null {
  if (raw == null || raw.trim().length === 0) {
    return null;
  }
  const parsed = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return null;
  }
  return parsed;
}

export function resolveSpawnTaskPingDefaults(): SpawnTaskPingDefaults {
  return {
    pingIntervalMs: parseEnvPositiveMs(process.env[TASK_PING_INTERVAL_MS_ENV]),
    pingResetOnActivity: parseEnvBoolean(process.env[TASK_PING_RESET_ON_ACTIVITY_ENV], true),
    defaultPersistent: parseEnvBoolean(process.env[TASK_PING_DEFAULT_PERSISTENT_ENV], false),
  };
}

export function resolveEffectivePingIntervalMs(
  taskPingMs: number | null | undefined,
  spawnDefaultMs: number | null,
): number {
  if (typeof taskPingMs === "number" && Number.isFinite(taskPingMs) && taskPingMs > 0) {
    return Math.trunc(taskPingMs);
  }
  if (typeof spawnDefaultMs === "number" && Number.isFinite(spawnDefaultMs) && spawnDefaultMs > 0) {
    return Math.trunc(spawnDefaultMs);
  }
  return DEFAULT_TASK_PING_INTERVAL_MS;
}
