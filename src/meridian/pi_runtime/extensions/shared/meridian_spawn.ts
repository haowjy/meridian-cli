import { basename } from "node:path";

import { splitShellCommands } from "./shell_split";

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

// `meridian` global options and whether each takes a separate value.
const MERIDIAN_GLOBAL_OPTIONS = new Map([["-C", true], ["--directory", true], ["--format", true]]);

/** True when the whole command is one `meridian spawn ...` invocation, optionally after global
 * options (`-C dir`, `--format x`). Conservative: anything the shell splitter cannot read, any
 * separator, or any quoting before the subcommand is not a match. */
export function isSingleMeridianSpawnCommand(command: string): boolean {
  const split = splitShellCommands(command);
  if (!split.complete || split.commands.length !== 1) return false;
  const words = split.commands[0].split(/\s+/);
  if (words[0] === undefined || basename(words[0]) !== "meridian") return false;
  for (let index = 1; index < words.length; index += 1) {
    const word = words[index];
    if (word === "spawn") return true;
    if (/['"]/u.test(word)) return false;
    if (MERIDIAN_GLOBAL_OPTIONS.get(word)) index += 1;
    else if (!/^--[a-z-]+=/u.test(word)) return false;
  }
  return false;
}
