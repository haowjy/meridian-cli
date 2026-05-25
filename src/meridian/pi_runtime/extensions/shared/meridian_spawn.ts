/** Detect `meridian spawn` CLI invocations in shell command strings. */
export const MERIDIAN_SPAWN_COMMAND_PATTERN = /\bmeridian\s+spawn\b/;

export function isMeridianSpawnCommand(command: string): boolean {
  return MERIDIAN_SPAWN_COMMAND_PATTERN.test(command);
}

/** `meridian spawn wait` (including `uv run meridian spawn wait`, env prefixes). */
export function isSpawnWaitCommand(command: string): boolean {
  return /\bmeridian\s+spawn\s+wait\b/.test(command);
}

/** Spawn launcher, not wait/list/show — matches `uv run meridian spawn -m …`. */
export function isSpawnLauncherCommand(command: string): boolean {
  return isMeridianSpawnCommand(command) && !isSpawnWaitCommand(command);
}

/** CLI background note: `Spawn id: p1234` ([_background_wait_note](models.py)). */
export const SPAWN_ID_FROM_LAUNCHER_LOG_PATTERN = /Spawn id:\s*(p\d+)/i;

export function extractSpawnIdFromLauncherLog(text: string): string | null {
  const match = text.match(SPAWN_ID_FROM_LAUNCHER_LOG_PATTERN);
  const id = match?.[1];
  if (!id || !isMeridianSpawnId(id)) {
    return null;
  }
  return id;
}

/** Meridian spawn IDs emitted by the CLI (e.g. `p1234`). */
export const MERIDIAN_SPAWN_ID_PATTERN = /\bp\d+\b/g;

export function isMeridianSpawnId(value: string): boolean {
  return /^p\d+$/.test(value);
}

export function extractMeridianSpawnIdsFromText(text: string): string[] {
  if (text.length === 0) {
    return [];
  }

  const ids = new Set<string>();
  MERIDIAN_SPAWN_ID_PATTERN.lastIndex = 0;
  for (const match of text.matchAll(MERIDIAN_SPAWN_ID_PATTERN)) {
    const id = match[0];
    if (isMeridianSpawnId(id)) {
      ids.add(id);
    }
  }
  return [...ids];
}
