import type { PsRow } from "./types";

const LIVE_TASK = new Set(["running"]);

export function formatPsRow(row: PsRow): string {
  if (row.kind === "meridian_spawn") {
    const summary = row.summary ? ` ${row.summary}` : "";
    const task = row.task_id ? ` task=${row.task_id}` : "";
    return `[spawn] ${row.spawn_id} ${row.status}${task}${summary}`;
  }
  const badge = row.kind === "meridian_spawn_wrapper" ? "wrapper" : "task";
  const pid = row.pid != null ? ` pid=${row.pid}` : "";
  return `[${badge}] ${row.task_id} ${row.status} ${row.label}${pid}`;
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
  return rows.find((row) => rowKey(row) === trimmed);
}

export function isLiveTaskRow(row: PsRow): boolean {
  return row.kind !== "meridian_spawn" && LIVE_TASK.has(row.status);
}

export function isLiveSpawnRow(row: PsRow): boolean {
  if (row.kind !== "meridian_spawn") {
    return false;
  }
  const status = row.status.toLowerCase();
  return !["succeeded", "failed", "cancelled", "canceled", "done", "exited"].includes(status);
}
