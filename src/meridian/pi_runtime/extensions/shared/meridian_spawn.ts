/** Detect `meridian spawn` CLI invocations in shell command strings. */
export const MERIDIAN_SPAWN_COMMAND_PATTERN = /\bmeridian\s+spawn\b/;

export function isMeridianSpawnCommand(command: string): boolean {
  return MERIDIAN_SPAWN_COMMAND_PATTERN.test(command);
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
