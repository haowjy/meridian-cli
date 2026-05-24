import type { ChildProcess } from "node:child_process";
import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";

import type { BackgroundTaskRecord, TaskIngress, WaitPolicy } from "./types";
import { MAX_BG_READ_BYTES, MAX_LOG_BYTES, type StoredTaskRecord } from "./task_constants";
import type { TaskEvents } from "./task_events";
import type { TaskPing } from "./task_ping";
import type { TaskStore } from "./task_store";
import type { RuntimeTask } from "./task_process_types";
import {
  clamp,
  isProcessAlive,
  killProcessTree,
  killProcessTreeHard,
  makeId,
  nowMs,
} from "./task_utils";
import type { SpawnTaskPingDefaults } from "./session_ping";

export class TaskProcess {
  private readonly waitReleases = new Map<string, () => void>();
  private pollTimer: NodeJS.Timeout | null = null;

  constructor(
    private readonly jobs: Map<string, RuntimeTask>,
    private readonly store: TaskStore,
    private readonly events: TaskEvents,
    private readonly ping: TaskPing,
    private readonly spawnPingDefaults: SpawnTaskPingDefaults,
  ) {}

  startPoller(): void {
    if (this.pollTimer != null) {
      return;
    }
    this.pollTimer = setInterval(() => {
      void this.pollForOrphanCompletions();
    }, 2_000);
    this.pollTimer.unref?.();
  }

  stopPoller(): void {
    if (this.pollTimer == null) {
      return;
    }
    clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  enqueueLogWrite(runtimeJob: RuntimeTask, work: () => Promise<void>): void {
    runtimeJob.logWriteChain = runtimeJob.logWriteChain.then(work).catch(() => undefined);
  }

  async closeLogHandle(runtimeJob: RuntimeTask): Promise<void> {
    await runtimeJob.logWriteChain.catch(() => undefined);
    if (runtimeJob.logHandle == null || runtimeJob.logHandleClosed) {
      return;
    }
    runtimeJob.logHandleClosed = true;
    try {
      await runtimeJob.logHandle.close();
    } catch {
      // ignore close errors
    }
  }

  attachChildLifecycle(runtimeJob: RuntimeTask): void {
    const { child, record } = runtimeJob;
    if (child == null) {
      return;
    }

    child.on("close", (exitCode, signal) => {
      void this.finishJob(record.task_id, {
        exitCode: exitCode ?? null,
        signal: signal ?? null,
      });
    });
  }

  async finishJob(
    jobId: string,
    outcome: { exitCode: number | null; signal: string | number | null },
  ): Promise<BackgroundTaskRecord | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }
    const record = runtimeJob.record;
    if (record.status !== "running") {
      return this.store.toPublicRecord(record);
    }

    const endMs = nowMs();
    record.ended_at_ms = endMs;
    record.duration_ms = Math.max(0, endMs - record.started_at_ms);
    record.exit_code = outcome.exitCode;
    record.signal = outcome.signal;
    record.success = outcome.exitCode === 0;
    record.status = outcome.signal ? "killed" : "exited";

    await this.closeLogHandle(runtimeJob);

    try {
      const stat = await fs.stat(record.combined_log_path);
      record.log_bytes = stat.size;
    } catch {
      // ignore
    }

    await this.store.enforceLogCap(record);
    await this.store.persistRecord(record);
    if (record.emitted_start) {
      this.events.emitTaskEnd(record);
    }
    this.ping.clearTaskOutputNotify(jobId);
    runtimeJob.resolveCompletion(record);
    return this.store.toPublicRecord(record);
  }

  async orphanScan(): Promise<void> {
    const entries = await fs.readdir(this.store.tasksDir, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      if (!entry.isDirectory()) {
        continue;
      }
      const record = await this.store.loadRecord(this.store.taskMetaPath(entry.name));
      if (record == null) {
        continue;
      }

      if (record.status !== "running") {
        const finished = this.store.createRuntimeTask(record, null);
        finished.resolveCompletion(record);
        this.jobs.set(record.task_id, finished);
        continue;
      }

      const runtimeJob = this.store.createRuntimeTask(record, null);
      this.jobs.set(record.task_id, runtimeJob);

      if (!isProcessAlive(record.pid)) {
        await this.finishJob(record.task_id, {
          exitCode: record.exit_code,
          signal: record.signal ?? "orphan-exited",
        });
      }
    }
  }

  private async pollForOrphanCompletions(): Promise<void> {
    for (const runtimeJob of this.jobs.values()) {
      const record = runtimeJob.record;
      if (record.status !== "running") {
        continue;
      }
      if (runtimeJob.child != null) {
        continue;
      }
      if (isProcessAlive(record.pid)) {
        continue;
      }
      await this.finishJob(record.task_id, {
        exitCode: record.exit_code,
        signal: record.signal ?? "orphan-exited",
      });
    }
  }

  async startJob(
    command: string,
    waitPolicy: WaitPolicy,
    cwd: string,
    env: Record<string, string>,
    label?: string,
    options?: {
      pingIntervalMs?: number | null;
      persistent?: boolean;
      ingress?: TaskIngress;
      onChunk?: (chunk: Buffer) => void;
    },
  ): Promise<{ runtimeJob: RuntimeTask; stdoutTail: string[]; stderrTail: string[] }> {
    const jobId = makeId("t");
    const startedAt = nowMs();
    await fs.mkdir(this.store.taskDir(jobId), { recursive: true });
    const logPath = this.store.taskLogPath(jobId);
    const logHandle = await fs.open(logPath, "a");

    const child = spawn("bash", ["-lc", command], {
      cwd,
      env,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    if (child.pid == null) {
      await logHandle.close();
      throw new Error("background-tasks: failed to start child process");
    }

    const record: StoredTaskRecord = {
      task_id: jobId,
      label: label?.trim() || command.slice(0, 48),
      command,
      cwd,
      wait_policy: waitPolicy,
      status: "running",
      pid: child.pid,
      started_at_ms: startedAt,
      ended_at_ms: null,
      duration_ms: null,
      exit_code: null,
      signal: null,
      success: null,
      stdout_log_path: logPath,
      stderr_log_path: logPath,
      combined_log_path: logPath,
      log_path: logPath,
      log_bytes: 0,
      log_truncated: false,
      emitted_start: false,
      ingress: options?.ingress ?? "background_task",
      persistent: options?.persistent ?? this.spawnPingDefaults.defaultPersistent,
      ping_interval_ms:
        typeof options?.pingIntervalMs === "number" && options.pingIntervalMs > 0
          ? Math.trunc(options.pingIntervalMs)
          : null,
    };
    this.ping.initializePingSchedule(record, startedAt);

    const runtimeJob = this.store.createRuntimeTask(record, child, logHandle);
    this.jobs.set(jobId, runtimeJob);
    await this.store.persistRecord(record);

    const stdoutTail: string[] = [];
    const stderrTail: string[] = [];

    const appendChunk = (chunk: Buffer, target: string[]): void => {
      const text = chunk.toString("utf-8");
      target.push(text);
      while (target.length > 32) {
        target.shift();
      }
      options?.onChunk?.(chunk);

      this.enqueueLogWrite(runtimeJob, async () => {
        if (runtimeJob.logHandle == null || runtimeJob.logHandleClosed) {
          return;
        }
        try {
          await runtimeJob.logHandle.appendFile(chunk);
          const stat = await runtimeJob.logHandle.stat();
          record.log_bytes = stat.size;
        } catch {
          // ignore logging errors
        }

        if (record.log_bytes > MAX_LOG_BYTES + 64 * 1024) {
          await this.store.enforceLogCap(record);
        }
        this.ping.bumpTaskActivity(record.task_id);
        this.ping.notifyTaskOutput(record.task_id);
      });
    };

    child.stdout?.on("data", (chunk) => {
      appendChunk(Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk)), stdoutTail);
    });
    child.stderr?.on("data", (chunk) => {
      appendChunk(Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk)), stderrTail);
    });

    this.attachChildLifecycle(runtimeJob);

    return {
      runtimeJob,
      stdoutTail,
      stderrTail,
    };
  }

  async detachJob(jobId: string): Promise<BackgroundTaskRecord | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }
    if (runtimeJob.record.emitted_start) {
      return this.store.toPublicRecord(runtimeJob.record);
    }

    runtimeJob.record.emitted_start = true;
    this.events.emitTaskStart(runtimeJob.record);

    if (runtimeJob.record.status !== "running") {
      this.events.emitTaskEnd(runtimeJob.record);
    }

    await this.store.persistRecord(runtimeJob.record);
    return this.store.toPublicRecord(runtimeJob.record);
  }

  releaseWait(jobId: string): boolean {
    const release = this.waitReleases.get(jobId);
    if (!release) {
      return false;
    }
    this.waitReleases.delete(jobId);
    release();
    return true;
  }

  async waitForCompletion(jobId: string, timeoutMs: number): Promise<BackgroundTaskRecord | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }
    this.ping.bumpTaskActivity(jobId);
    if (runtimeJob.record.status !== "running") {
      return this.store.toPublicRecord(runtimeJob.record);
    }

    const releasedEarly = new Promise<StoredTaskRecord>((resolve) => {
      this.waitReleases.set(jobId, () => {
        resolve(runtimeJob.record);
      });
    });

    try {
      const done = await Promise.race<StoredTaskRecord>([
        runtimeJob.completion,
        releasedEarly,
        delay(timeoutMs).then(() => {
          throw new Error("wait-timeout");
        }),
      ]);
      return this.store.toPublicRecord(done);
    } catch {
      return this.store.toPublicRecord(runtimeJob.record);
    } finally {
      this.waitReleases.delete(jobId);
    }
  }

  async syncBashToolRunning(input: {
    taskId: string;
    command: string;
    pid: number | null;
    waitPolicy: unknown;
    cwd?: string;
    logPath?: string;
    pingIntervalMs?: number;
    persistent?: boolean;
  }): Promise<BackgroundTaskRecord | null> {
    const waitPolicy = input.waitPolicy === "detached" ? "detached" : "tracked";
    const existing = this.jobs.get(input.taskId);
    const startedAt = nowMs();
    const cwd = input.cwd?.trim() || process.cwd();
    const logPath = input.logPath?.trim() || this.store.taskLogPath(input.taskId);

    if (existing == null) {
      await fs.mkdir(this.store.taskDir(input.taskId), { recursive: true });
      const record: StoredTaskRecord = {
        task_id: input.taskId,
        label: input.command.trim().slice(0, 48) || input.taskId,
        command: input.command,
        cwd,
        wait_policy: waitPolicy,
        status: "running",
        pid: input.pid,
        started_at_ms: startedAt,
        ended_at_ms: null,
        duration_ms: null,
        exit_code: null,
        signal: null,
        success: null,
        stdout_log_path: logPath,
        stderr_log_path: logPath,
        combined_log_path: logPath,
        log_path: logPath,
        log_bytes: 0,
        log_truncated: false,
        emitted_start: false,
        ingress: "bash",
        persistent: input.persistent ?? this.spawnPingDefaults.defaultPersistent,
        ping_interval_ms:
          typeof input.pingIntervalMs === "number" && input.pingIntervalMs > 0
            ? Math.trunc(input.pingIntervalMs)
            : null,
      };
      this.ping.initializePingSchedule(record, startedAt);
      const runtimeJob = this.store.createRuntimeTask(record, null);
      this.jobs.set(input.taskId, runtimeJob);
      await this.store.persistRecord(record);
    } else {
      const record = existing.record;
      record.command = input.command || record.command;
      record.pid = input.pid ?? record.pid;
      record.wait_policy = waitPolicy;
      if (typeof input.pingIntervalMs === "number" && input.pingIntervalMs > 0) {
        record.ping_interval_ms = Math.trunc(input.pingIntervalMs);
      }
      if (input.persistent === true) {
        record.persistent = true;
      }
      await this.store.persistRecord(record);
    }

    return this.detachJob(input.taskId);
  }

  async syncBashToolExited(input: {
    taskId: string;
    exitCode: number | null;
    signal: string | number | null;
  }): Promise<BackgroundTaskRecord | null> {
    const runtimeJob = this.jobs.get(input.taskId);
    if (runtimeJob == null) {
      return null;
    }
    if (runtimeJob.record.status !== "running") {
      return this.store.toPublicRecord(runtimeJob.record);
    }
    return this.finishJob(input.taskId, {
      exitCode: input.exitCode,
      signal: input.signal,
    });
  }

  async readLog(
    jobId: string,
    maxBytes: number,
    offset?: number,
  ): Promise<{
    data: string;
    log_truncated: boolean;
    next_offset: number;
    eof: boolean;
  } | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }

    this.ping.bumpTaskActivity(jobId);
    const record = runtimeJob.record;
    let statSize = 0;
    try {
      const stat = await fs.stat(record.combined_log_path);
      statSize = stat.size;
    } catch {
      return {
        data: "",
        log_truncated: record.log_truncated,
        next_offset: 0,
        eof: true,
      };
    }

    if (statSize <= 0) {
      return {
        data: "",
        log_truncated: record.log_truncated,
        next_offset: 0,
        eof: true,
      };
    }

    const capped = clamp(maxBytes, 1, MAX_BG_READ_BYTES);
    const start = typeof offset === "number" && offset >= 0 ? offset : Math.max(0, statSize - capped);
    const safeStart = clamp(start, 0, statSize);
    const bytesToRead = clamp(capped, 0, statSize - safeStart);

    const fd = await fs.open(record.combined_log_path, "r");
    try {
      const buffer = Buffer.allocUnsafe(bytesToRead);
      const { bytesRead } = await fd.read(buffer, 0, bytesToRead, safeStart);
      const nextOffset = safeStart + bytesRead;
      return {
        data: buffer.subarray(0, bytesRead).toString("utf-8"),
        log_truncated: record.log_truncated,
        next_offset: nextOffset,
        eof: nextOffset >= statSize && record.status !== "running",
      };
    } finally {
      await fd.close();
    }
  }

  async killJob(jobId: string): Promise<BackgroundTaskRecord | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }
    this.ping.bumpTaskActivity(jobId);
    if (runtimeJob.record.status !== "running") {
      return this.store.toPublicRecord(runtimeJob.record);
    }

    killProcessTree(runtimeJob.record.pid);
    await delay(500);
    if (isProcessAlive(runtimeJob.record.pid)) {
      await killProcessTreeHard(runtimeJob.record.pid);
    }

    return this.finishJob(jobId, {
      exitCode: null,
      signal: "killed",
    });
  }

  async shutdownCleanup(): Promise<void> {
    this.stopPoller();
    for (const runtimeJob of this.jobs.values()) {
      const record = runtimeJob.record;
      if (record.status !== "running") {
        continue;
      }
      if (record.wait_policy === "detached") {
        continue;
      }
      await this.killJob(record.task_id);
    }
  }
}
