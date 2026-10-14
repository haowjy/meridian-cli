/** Mirror of core/refs.py: user refs, not filesystem discovery. */
export type MeridianRef = {
  kind: "spawn" | "run" | "chat" | "native";
  value: string;
  spawnId?: string;
  runId?: string;
  run?: number;
};

export function parseRef(text: string): MeridianRef {
  const value = text.trim();
  const match = /^p([0-9]+)(?:\.([0-9]+))?$/.exec(value);
  if (match && (match[2] === undefined || BigInt(match[2]) >= 1n)) {
    const spawnId = `p${match[1]}`;
    const run = match[2] === undefined ? 1n : BigInt(match[2]);
    return { kind: match[2] === undefined ? "spawn" : "run", value, spawnId,
      runId: run === 1n ? spawnId : `${spawnId}.${run}`, run: Number(run) };
  }
  return { kind: /^c[0-9]+$/.test(value) ? "chat" : "native", value };
}

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
export const SPAWN_ID_FROM_LAUNCHER_LOG_PATTERN = /Spawn id:\s*(p[0-9]+(?:\.[0-9]+)?)(?![\w.])/i;

export function extractSpawnIdFromLauncherLog(text: string): string | null {
  const match = text.match(SPAWN_ID_FROM_LAUNCHER_LOG_PATTERN);
  const id = match?.[1];
  if (!id || parseRef(id).spawnId === undefined) {
    return null;
  }
  return parseRef(id).spawnId ?? null;
}

/** Meridian spawn IDs emitted by the CLI (e.g. `p1234`). */
export const MERIDIAN_SPAWN_ID_PATTERN = /\bp[0-9]+(?:\.[0-9]+)?\b/g;

export function isMeridianSpawnId(value: string): boolean {
  return parseRef(value).kind === "spawn";
}

export function extractMeridianSpawnIdsFromText(text: string): string[] {
  if (text.length === 0) {
    return [];
  }

  const ids = new Set<string>();
  MERIDIAN_SPAWN_ID_PATTERN.lastIndex = 0;
  for (const match of text.matchAll(MERIDIAN_SPAWN_ID_PATTERN)) {
    const id = match[0];
    const ref = parseRef(id);
    if (ref.runId) ids.add(ref.runId);
  }
  return [...ids];
}
