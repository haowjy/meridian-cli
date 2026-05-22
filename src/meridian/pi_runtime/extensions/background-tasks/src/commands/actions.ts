import { runMeridianCommand } from "../../../shared/meridian_cli";
import { DEFAULT_BG_READ_BYTES } from "../task_registry";
import type { TaskRegistry } from "../task_registry";
import {
  findPsRow,
  formatPsTable,
  isLiveSpawnRow,
  isLiveTaskRow,
  rowKey,
} from "../format_rows";
import type { PsRow } from "../types";

export async function listUnifiedRows(
  registry: TaskRegistry,
  mergeRows: (tasks: Awaited<ReturnType<TaskRegistry["list"]>>) => PsRow[],
  includeCompleted = true,
): Promise<PsRow[]> {
  const tasks = await registry.list(includeCompleted);
  return mergeRows(tasks);
}

export async function killPsRow(
  registry: TaskRegistry,
  row: PsRow,
): Promise<{ ok: boolean; message: string }> {
  if (row.kind === "meridian_spawn") {
    if (!isLiveSpawnRow(row)) {
      return { ok: false, message: `Spawn ${row.spawn_id} is not running.` };
    }
    const result = await runMeridianCommand(["spawn", "cancel", row.spawn_id]);
    const text = (result.stdout || result.stderr).trim() || `cancelled ${row.spawn_id}`;
    return { ok: result.exitCode === 0, message: text };
  }
  if (!isLiveTaskRow(row)) {
    return { ok: false, message: `Task ${row.task_id} is not running.` };
  }
  const record = await registry.killJob(row.task_id);
  if (!record) {
    return { ok: false, message: `Task ${row.task_id} not found.` };
  }
  return { ok: true, message: `Killed task ${row.task_id}.` };
}

export async function readPsRowLogs(
  registry: TaskRegistry,
  row: PsRow,
  maxBytes = DEFAULT_BG_READ_BYTES,
): Promise<{ ok: boolean; message: string }> {
  if (row.kind === "meridian_spawn") {
    if (row.task_id) {
      const log = await registry.readLog(row.task_id, maxBytes);
      if (log) {
        return { ok: true, message: log.data || "(empty log)" };
      }
    }
    const result = await runMeridianCommand(["spawn", "show", row.spawn_id, "--no-report"]);
    const text = (result.stdout || result.stderr).trim() || `No output for spawn ${row.spawn_id}`;
    return { ok: result.exitCode === 0, message: text };
  }
  const log = await registry.readLog(row.task_id, maxBytes);
  if (!log) {
    return { ok: false, message: `Task ${row.task_id} not found.` };
  }
  const header = `--- ${row.combined_log_path} ---\n`;
  return { ok: true, message: header + (log.data || "(empty log)") };
}

export function resolveTargetRow(rows: PsRow[], arg: string): PsRow | null {
  const trimmed = arg.trim();
  if (trimmed) {
    return findPsRow(rows, trimmed) ?? null;
  }
  const live = rows.filter((row) => isLiveTaskRow(row) || isLiveSpawnRow(row));
  if (live.length === 1) {
    return live[0] ?? null;
  }
  if (live.length === 0) {
    return null;
  }
  return null;
}

export function pickHint(rows: PsRow[], verb: string): string {
  const live = rows.filter((row) => isLiveTaskRow(row) || isLiveSpawnRow(row));
  if (live.length === 0) {
    return `No running rows to ${verb}.`;
  }
  return `${verb} requires an id when multiple rows are live:\n${formatPsTable(live)}\nKeys: ${live.map(rowKey).join(", ")}`;
}
