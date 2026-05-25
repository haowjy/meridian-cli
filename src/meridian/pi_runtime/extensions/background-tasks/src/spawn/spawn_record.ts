import { runMeridianCommand } from "../../../shared/meridian_cli";
import { isMeridianSpawnId } from "../../../shared/meridian_spawn";

export const ACTIVE_SPAWN_STATUSES = new Set(["queued", "running", "finalizing"]);

export type ConfirmedSpawnRecord = {
  spawn_id: string;
  status: string;
  task_id?: string;
  summary?: string;
};

export function parseSpawnStatus(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }
  try {
    const parsed = JSON.parse(trimmed) as { status?: string };
    if (typeof parsed.status === "string") {
      return parsed.status.toLowerCase();
    }
  } catch {
    // fall through
  }
  const match = trimmed.match(
    /\b(queued|running|starting|finalizing|succeeded|failed|cancelled|canceled)\b/i,
  );
  return match?.[1]?.toLowerCase() ?? null;
}

function parseSpawnShowPayload(text: string): ConfirmedSpawnRecord | null {
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }
  try {
    const parsed = JSON.parse(trimmed) as {
      spawn_id?: string;
      id?: string;
      status?: string;
      task_id?: string;
      summary?: string;
      label?: string;
    };
    const spawnId =
      typeof parsed.spawn_id === "string"
        ? parsed.spawn_id
        : typeof parsed.id === "string"
          ? parsed.id
          : null;
    if (!spawnId || !isMeridianSpawnId(spawnId)) {
      return null;
    }
    const status = typeof parsed.status === "string" ? parsed.status.toLowerCase() : null;
    if (!status) {
      return null;
    }
    return {
      spawn_id: spawnId,
      status,
      task_id: typeof parsed.task_id === "string" ? parsed.task_id : undefined,
      summary:
        typeof parsed.summary === "string"
          ? parsed.summary
          : typeof parsed.label === "string"
            ? parsed.label
            : undefined,
    };
  } catch {
    const status = parseSpawnStatus(trimmed);
    const idMatch = trimmed.match(/\bp\d+\b/);
    const spawnId = idMatch?.[0];
    if (!spawnId || !isMeridianSpawnId(spawnId) || !status) {
      return null;
    }
    return { spawn_id: spawnId, status };
  }
}

function parseSpawnListPayload(text: string): string[] {
  const trimmed = text.trim();
  if (!trimmed) {
    return [];
  }
  try {
    const parsed = JSON.parse(trimmed) as {
      spawns?: Array<{ spawn_id?: string; id?: string }>;
    };
    if (!Array.isArray(parsed.spawns)) {
      return [];
    }
    const ids: string[] = [];
    for (const row of parsed.spawns) {
      const id =
        typeof row.spawn_id === "string"
          ? row.spawn_id
          : typeof row.id === "string"
            ? row.id
            : null;
      if (id && isMeridianSpawnId(id)) {
        ids.push(id);
      }
    }
    return ids;
  } catch {
    return [];
  }
}

export async function fetchSpawnListIds(): Promise<string[]> {
  const result = await runMeridianCommand(["--json", "spawn", "list"], 15_000);
  if ((result.exitCode ?? 1) !== 0) {
    return [];
  }
  return parseSpawnListPayload(result.stdout);
}

export async function confirmSpawnRecord(spawnId: string): Promise<ConfirmedSpawnRecord | null> {
  if (!isMeridianSpawnId(spawnId)) {
    return null;
  }
  const result = await runMeridianCommand(
    ["--json", "spawn", "show", spawnId, "--no-report"],
    15_000,
  );
  if ((result.exitCode ?? 1) !== 0) {
    return null;
  }
  const text = (result.stdout || result.stderr).trim();
  const record = parseSpawnShowPayload(text);
  if (!record) {
    return null;
  }
  return { ...record, spawn_id: spawnId };
}

export function isActiveSpawnStatus(status: string): boolean {
  return ACTIVE_SPAWN_STATUSES.has(status.toLowerCase());
}
