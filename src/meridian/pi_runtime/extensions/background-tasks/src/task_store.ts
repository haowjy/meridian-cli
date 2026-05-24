import { promises as fs } from "node:fs";
import path from "node:path";

import type { BackgroundTaskRecord } from "./types";
import { MAX_LOG_BYTES, type StoredTaskRecord } from "./task_constants";
import type { RuntimeTask } from "./task_process_types";

export class TaskStore {
  readonly tasksDir: string;

  constructor(
    stateRoot: string,
    sessionId: string,
  ) {
    this.tasksDir = path.join(stateRoot, "background-tasks", sessionId, "tasks");
  }

  async ensureTasksDir(): Promise<void> {
    await fs.mkdir(this.tasksDir, { recursive: true });
  }

  taskDir(taskId: string): string {
    return path.join(this.tasksDir, taskId);
  }

  taskMetaPath(taskId: string): string {
    return path.join(this.taskDir(taskId), "meta.json");
  }

  taskLogPath(taskId: string): string {
    return path.join(this.taskDir(taskId), "combined.log");
  }

  async persistRecord(record: StoredTaskRecord): Promise<void> {
    const finalPath = this.taskMetaPath(record.task_id);
    const tempPath = `${finalPath}.tmp-${process.pid}-${Math.random().toString(16).slice(2)}`;
    await fs.writeFile(tempPath, `${JSON.stringify(record)}\n`, "utf-8");
    await fs.rename(tempPath, finalPath);
  }

  async loadRecord(filePath: string): Promise<StoredTaskRecord | null> {
    try {
      const raw = await fs.readFile(filePath, "utf-8");
      const parsed = JSON.parse(raw) as StoredTaskRecord;
      if (!parsed || typeof parsed !== "object") {
        return null;
      }
      if (typeof parsed.task_id !== "string") {
        return null;
      }
      if (typeof parsed.emitted_start !== "boolean") {
        parsed.emitted_start = false;
      }
      if (parsed.ingress !== "bash" && parsed.ingress !== "background_task") {
        parsed.ingress = "background_task";
      }
      if (typeof parsed.persistent !== "boolean") {
        parsed.persistent = false;
      }
      return parsed;
    } catch {
      return null;
    }
  }

  toPublicRecord(record: StoredTaskRecord): BackgroundTaskRecord {
    return {
      task_id: record.task_id,
      label: record.label,
      command: record.command,
      cwd: record.cwd,
      pid: record.pid,
      wait_policy: record.wait_policy,
      status: record.status,
      success: record.success,
      exit_code: record.exit_code,
      signal: record.signal,
      started_at_ms: record.started_at_ms,
      ended_at_ms: record.ended_at_ms,
      stdout_log_path: record.stdout_log_path,
      stderr_log_path: record.stderr_log_path,
      combined_log_path: record.combined_log_path,
      log_bytes: record.log_bytes,
      log_truncated: record.log_truncated,
      ingress: record.ingress,
      persistent: record.persistent,
      ping_interval_ms: record.ping_interval_ms,
      last_activity_at_ms: record.last_activity_at_ms,
      next_ping_at_ms: record.next_ping_at_ms,
    };
  }

  createRuntimeTask(
    record: StoredTaskRecord,
    child: import("node:child_process").ChildProcess | null,
    logHandle: Awaited<ReturnType<typeof fs.open>> | null = null,
  ): RuntimeTask {
    let resolveCompletion!: (value: BackgroundTaskRecord) => void;
    const completion = new Promise<BackgroundTaskRecord>((resolve) => {
      resolveCompletion = resolve;
    });
    return {
      record,
      child,
      completion,
      resolveCompletion,
      logHandle,
      logHandleClosed: logHandle == null,
      logWriteChain: Promise.resolve(),
    };
  }

  async enforceLogCap(record: StoredTaskRecord): Promise<void> {
    try {
      const stat = await fs.stat(record.combined_log_path);
      if (stat.size <= MAX_LOG_BYTES) {
        record.log_bytes = stat.size;
        await this.persistRecord(record);
        return;
      }

      const fd = await fs.open(record.combined_log_path, "r");
      try {
        const start = stat.size - MAX_LOG_BYTES;
        const buffer = Buffer.allocUnsafe(MAX_LOG_BYTES);
        await fd.read(buffer, 0, MAX_LOG_BYTES, start);
        const tmpPath = `${record.combined_log_path}.tmp-${process.pid}-${Math.random().toString(16).slice(2)}`;
        await fs.writeFile(tmpPath, buffer);
        await fs.rename(tmpPath, record.combined_log_path);
      } finally {
        await fd.close();
      }

      record.log_bytes = MAX_LOG_BYTES;
      record.log_truncated = true;
      await this.persistRecord(record);
    } catch {
      // ignore cap errors
    }
  }

  async deleteTaskDir(taskId: string): Promise<void> {
    await fs.rm(this.taskDir(taskId), { recursive: true, force: true }).catch(() => undefined);
  }

  /** Remove finished tasks from memory and delete persisted task directories. */
  async clearFinished(jobs: Map<string, RuntimeTask>): Promise<number> {
    let removed = 0;
    for (const [taskId, runtimeJob] of [...jobs.entries()]) {
      if (runtimeJob.record.status === "running") {
        continue;
      }
      jobs.delete(taskId);
      await this.deleteTaskDir(taskId);
      removed += 1;
    }

    const entries = await fs.readdir(this.tasksDir, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      if (!entry.isDirectory()) {
        continue;
      }
      if (jobs.has(entry.name)) {
        continue;
      }
      const record = await this.loadRecord(this.taskMetaPath(entry.name));
      if (record == null || record.status === "running") {
        continue;
      }
      await this.deleteTaskDir(entry.name);
      removed += 1;
    }

    return removed;
  }
}
