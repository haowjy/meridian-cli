import { EventEmitter } from "node:events";
import { readFileSync, statSync } from "node:fs";

import type { MeridianEventBus } from "../../../shared/meridian_event_bus";
import {
  killPsRow,
  listUnifiedRows,
} from "../commands/actions";
import {
  findPsRow,
  isLiveSpawnRow,
  isLiveTaskRow,
  rowKey,
} from "../format_rows";
import {
  resolveEffectivePingIntervalMs,
  type SpawnTaskPingDefaults,
} from "../session_ping";
import type { TaskRegistry } from "../task_registry";
import type { BackgroundTaskRecord, PsRow } from "../types";
import { LIVE_PANEL_STATUSES, type PanelEntry, type PanelStatus } from "./types";

const REFRESH_CHANNELS = [
  "meridian:task:start",
  "meridian:task:end",
  "meridian:task:ping",
  "meridian:spawn:discovered",
  "meridian:spawn:updated",
  "meridian:spawn:removed",
] as const;

function readTailLines(filePath: string, tailLines: number): string[] {
  try {
    const content = readFileSync(filePath, "utf-8");
    const lines = content.split("\n");
    if (lines.length > 0 && lines[lines.length - 1] === "") {
      lines.pop();
    }
    if (lines.length <= tailLines) {
      return lines;
    }
    return lines.slice(-tailLines);
  } catch {
    return [];
  }
}

function mapTaskStatus(status: string): PanelStatus {
  if (status === "failed") {
    return "exited";
  }
  return status as PanelStatus;
}

export class TaskPanelHost {
  private readonly emitter = new EventEmitter();
  private readonly unsubscribers: Array<() => void> = [];

  constructor(
    private readonly getRegistry: () => TaskRegistry | null,
    private readonly mergeRows: (tasks: BackgroundTaskRecord[]) => PsRow[],
    bus: MeridianEventBus,
    private readonly spawnPingDefaults: SpawnTaskPingDefaults,
  ) {
    for (const channel of REFRESH_CHANNELS) {
      this.unsubscribers.push(
        bus.on(channel, () => {
          this.emitter.emit("change");
        }),
      );
    }
  }

  dispose(): void {
    for (const unsub of this.unsubscribers) {
      unsub();
    }
    this.unsubscribers.length = 0;
    this.emitter.removeAllListeners();
  }

  onEvent(listener: () => void): () => void {
    this.emitter.on("change", listener);
    return () => {
      this.emitter.off("change", listener);
    };
  }

  private rowToEntry(row: PsRow): PanelEntry {
    if (row.kind === "meridian_spawn") {
      const live = isLiveSpawnRow(row);
      return {
        id: row.spawn_id,
        rowKey: rowKey(row),
        kind: row.kind,
        name: row.spawn_id,
        command: row.summary?.trim() || `meridian spawn ${row.spawn_id}`,
        cwd: "",
        pid: -1,
        startTime: 0,
        endTime: live ? null : Date.now(),
        status: row.status,
        exitCode: null,
        success: live ? null : false,
        combinedLogPath: "",
        logBytes: 0,
        persistent: false,
        pingIntervalMs: null,
        nextPingAtMs: null,
        lastActivityAtMs: null,
        isLive: live,
      };
    }

    const pingIntervalMs = resolveEffectivePingIntervalMs(
      row.ping_interval_ms,
      this.spawnPingDefaults.pingIntervalMs,
    );

    return {
      id: row.task_id,
      rowKey: rowKey(row),
      kind: row.kind,
      name: row.label || row.task_id,
      command: row.command,
      cwd: row.cwd,
      pid: row.pid ?? -1,
      startTime: row.started_at_ms,
      endTime: row.ended_at_ms,
      status: mapTaskStatus(row.status),
      exitCode: row.exit_code,
      success: row.success,
      combinedLogPath: row.combined_log_path,
      logBytes: row.log_bytes,
      persistent: row.persistent === true,
      pingIntervalMs,
      nextPingAtMs: row.next_ping_at_ms ?? null,
      lastActivityAtMs: row.last_activity_at_ms ?? null,
      isLive: isLiveTaskRow(row),
    };
  }

  async list(): Promise<PanelEntry[]> {
    const registry = this.getRegistry();
    if (!registry) {
      return [];
    }
    const rows = await listUnifiedRows(registry, this.mergeRows, true);
    return rows
      .map((row) => this.rowToEntry(row))
      .sort((a, b) => b.startTime - a.startTime);
  }

  async get(id: string): Promise<PanelEntry | null> {
    const entries = await this.list();
    return entries.find((entry) => entry.id === id || entry.rowKey === id) ?? null;
  }

  getOutput(
    id: string,
    tailLines = 100,
  ): { stdout: string[]; stderr: string[]; status: string } | null {
    const entry = this.getSync(id);
    if (!entry?.combinedLogPath) {
      return null;
    }
    const lines = readTailLines(entry.combinedLogPath, tailLines);
    return {
      stdout: lines,
      stderr: [],
      status: entry.status,
    };
  }

  getCombinedOutput(
    id: string,
    tailLines = 100,
  ): { type: "stdout" | "stderr"; text: string }[] | null {
    const output = this.getOutput(id, tailLines);
    if (!output) {
      return null;
    }
    return output.stdout.map((text) => ({ type: "stdout" as const, text }));
  }

  getLogFiles(
    id: string,
  ): { stdoutFile: string; stderrFile: string; combinedFile: string } | null {
    const entry = this.getSync(id);
    if (!entry?.combinedLogPath) {
      return null;
    }
    return {
      stdoutFile: entry.combinedLogPath,
      stderrFile: entry.combinedLogPath,
      combinedFile: entry.combinedLogPath,
    };
  }

  getFileSize(id: string): { stdout: number; stderr: number } | null {
    const entry = this.getSync(id);
    if (!entry) {
      return null;
    }
    if (entry.combinedLogPath) {
      try {
        const size = statSync(entry.combinedLogPath).size;
        return { stdout: size, stderr: 0 };
      } catch {
        return { stdout: entry.logBytes, stderr: 0 };
      }
    }
    return { stdout: entry.logBytes, stderr: 0 };
  }

  async kill(
    id: string,
    _opts?: { signal?: NodeJS.Signals; timeoutMs?: number },
  ): Promise<{ ok: boolean; reason?: string }> {
    const registry = this.getRegistry();
    if (!registry) {
      return { ok: false, reason: "no_registry" };
    }
    const rows = await listUnifiedRows(registry, this.mergeRows, true);
    const target = findPsRow(rows, id);
    if (!target) {
      return { ok: false, reason: "not_found" };
    }
    const result = await killPsRow(registry, target);
    if (result.ok) {
      this.emitter.emit("change");
    }
    return { ok: result.ok, reason: result.ok ? undefined : "kill_failed" };
  }

  async clearFinished(): Promise<number> {
    const registry = this.getRegistry();
    if (!registry) {
      return 0;
    }
    const cleared = await registry.clearFinished();
    if (cleared > 0) {
      this.emitter.emit("change");
    }
    return cleared;
  }

  /** Cached sync lookup — list() is async; panel render uses last fetched list via caller. */
  private syncEntries: PanelEntry[] = [];

  setSyncEntries(entries: PanelEntry[]): void {
    this.syncEntries = entries;
  }

  listSync(): PanelEntry[] {
    return this.syncEntries;
  }

  private getSync(id: string): PanelEntry | null {
    return (
      this.syncEntries.find((entry) => entry.id === id || entry.rowKey === id) ?? null
    );
  }

  isLiveStatus(status: PanelStatus): boolean {
    return LIVE_PANEL_STATUSES.has(status) || status === "running";
  }
}
