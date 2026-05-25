import type { MeridianSpawnAttachment, PsRow } from "./types";

const LIVE_TASK = new Set(["running"]);

const TERMINAL_SPAWN = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "canceled",
  "done",
  "exited",
]);

export function isLiveSpawnAttachment(
  attachment: MeridianSpawnAttachment | undefined,
): boolean {
  if (!attachment) {
    return false;
  }
  return !TERMINAL_SPAWN.has(attachment.status.toLowerCase());
}

export function formatPsRow(row: PsRow): string {
  if (row.kind === "meridian_spawn") {
    const summary = row.summary ? ` ${row.summary}` : "";
    const task = row.task_id ? ` task=${row.task_id}` : "";
    return `[spawn] ${row.spawn_id} ${row.status}${task}${summary}`;
  }
  const spawn = row.meridian_spawn;
  const badge = spawn ? "task+spawn" : "task";
  const pid = row.pid != null ? ` pid=${row.pid}` : "";
  const spawnPart = spawn ? ` ${spawn.spawn_id} ${spawn.status}` : "";
  return `[${badge}] ${row.task_id}${spawnPart} ${row.status} ${row.label}${pid}`;
}

export function formatPsTable(rows: PsRow[]): string {
  if (rows.length === 0) {
    return "No processes or spawns.";
  }
  return rows.map(formatPsRow).join("\n");
}

export function rowKey(row: PsRow): string {
  if (row.kind === "meridian_spawn") {
    return row.spawn_id;
  }
  return row.task_id;
}

export function findPsRow(rows: PsRow[], id: string): PsRow | undefined {
  const trimmed = id.trim();
  if (!trimmed) {
    return undefined;
  }
  const direct = rows.find((row) => rowKey(row) === trimmed);
  if (direct) {
    return direct;
  }
  return rows.find(
    (row) => row.kind === "process" && row.meridian_spawn?.spawn_id === trimmed,
  );
}

export function isLiveTaskRow(row: PsRow): boolean {
  if (row.kind === "meridian_spawn") {
    return false;
  }
  if (LIVE_TASK.has(row.status)) {
    return true;
  }
  return isLiveSpawnAttachment(row.meridian_spawn);
}

export function isLiveSpawnRow(row: PsRow): boolean {
  if (row.kind === "meridian_spawn") {
    const status = row.status.toLowerCase();
    return !TERMINAL_SPAWN.has(status);
  }
  return isLiveSpawnAttachment(row.meridian_spawn);
}
