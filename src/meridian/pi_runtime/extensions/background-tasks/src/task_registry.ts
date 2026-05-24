import type { ChildProcess } from "node:child_process";
import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

import type { BackgroundTaskRecord, TaskIngress, TaskStatus, WaitPolicy } from "./types";
import { createLocalBus, type MeridianEventBus } from "../../shared/meridian_event_bus";
import type { LifecycleSidecarWriter } from "../../shared/lifecycle_sidecar";
import {
  resolveEffectivePingIntervalMs,
  resolveSpawnTaskPingDefaults,
  type SpawnTaskPingDefaults,
} from "./session_ping";
import { resolveStateRoot } from "../../shared/pi_state_paths";

export { resolveStateRoot };

type RuntimeTask = {
  record: BackgroundTaskRecord;
  child: ChildProcess | null;
  completion: Promise<BackgroundTaskRecord>;
  resolveCompletion: (value: BackgroundTaskRecord) => void;
  logHandle: Awaited<ReturnType<typeof fs.open>> | null;
  logHandleClosed: boolean;
  logWriteChain: Promise<void>;
};

const MAX_COMMAND_LENGTH = 512;
const MAX_FOREGROUND_TAIL_BYTES = 16 * 1024;
export const DEFAULT_BG_READ_BYTES = 8 * 1024;
export const MAX_BG_READ_BYTES = 64 * 1024;
const MAX_LOG_BYTES = 10 * 1024 * 1024;
export const DEFAULT_BG_WAIT_TIMEOUT_MS = 30_000;
export const MAX_BG_WAIT_TIMEOUT_MS = 10 * 60 * 1000;
const TASK_START_EVENT = "meridian:task:start";
const TASK_END_EVENT = "meridian:task:end";
const TASK_PING_EVENT = "meridian:task:ping";
const TASK_OUTPUT_EVENT = "meridian:task:output";
const TASK_OUTPUT_THROTTLE_MS = 100;
const SUBSPAWN_START_EVENT = "meridian:subspawn:start";
const SUBSPAWN_END_EVENT = "meridian:subspawn:end";
const MERIDIAN_SPAWN_COMMAND_PATTERN = /\bmeridian\s+spawn\b/;
const PING_SCAN_INTERVAL_MS = 60_000;

/** Internal record shape persisted to meta.json (superset of BackgroundTaskRecord). */
type StoredTaskRecord = BackgroundTaskRecord & {
  emitted_start: boolean;
  duration_ms: number | null;
  log_path: string;
  ingress: TaskIngress;
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function toInt(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  return fallback;
}

function truncateUtf8Tail(input: string, maxBytes: number): { text: string; truncated: boolean } {
  const buffer = Buffer.from(input, "utf-8");
  if (buffer.byteLength <= maxBytes) {
    return { text: input, truncated: false };
  }
  return {
    text: buffer.subarray(buffer.byteLength - maxBytes).toString("utf-8"),
    truncated: true,
  };
}

function trimCombinedTails(stdoutTail: string, stderrTail: string): {
  stdoutTail: string;
  stderrTail: string;
  outputTruncated: boolean;
} {
  const stdoutBytes = Buffer.byteLength(stdoutTail, "utf-8");
  const stderrBytes = Buffer.byteLength(stderrTail, "utf-8");
  const total = stdoutBytes + stderrBytes;
  if (total <= MAX_FOREGROUND_TAIL_BYTES) {
    return {
      stdoutTail,
      stderrTail,
      outputTruncated: false,
    };
  }

  const half = Math.floor(MAX_FOREGROUND_TAIL_BYTES / 2);
  const stdoutMax = Math.max(0, MAX_FOREGROUND_TAIL_BYTES - Math.min(stderrBytes, half));
  const stderrMax = Math.max(0, MAX_FOREGROUND_TAIL_BYTES - Math.min(stdoutBytes, half));
  const stdout = truncateUtf8Tail(stdoutTail, stdoutMax).text;
  const stderr = truncateUtf8Tail(stderrTail, stderrMax).text;

  return {
    stdoutTail: stdout,
    stderrTail: stderr,
    outputTruncated: true,
  };
}

function truncateCommand(command: string): string {
  const { text } = truncateUtf8Tail(command, MAX_COMMAND_LENGTH);
  return text;
}

function nowMs(): number {
  return Date.now();
}

function makeId(prefix: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `${prefix}-${nowMs().toString(36)}-${rand}`;
}

function normalizeWaitPolicy(value: unknown): WaitPolicy {
  return value === "detached" ? "detached" : "tracked";
}

function isMeridianSpawnCommand(command: string): boolean {
  return MERIDIAN_SPAWN_COMMAND_PATTERN.test(command);
}

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function killProcessTree(pid: number): void {
  if (process.platform === "win32") {
    // POSIX-first implementation. Windows process-tree kill is deferred follow-up.
    try {
      process.kill(pid, "SIGTERM");
    } catch {
      // ignore
    }
    return;
  }

  try {
    process.kill(-pid, "SIGTERM");
  } catch {
    try {
      process.kill(pid, "SIGTERM");
    } catch {
      // ignore
    }
  }
}

async function killProcessTreeHard(pid: number): Promise<void> {
  if (process.platform === "win32") {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ignore
    }
    return;
  }

  try {
    process.kill(-pid, "SIGKILL");
  } catch {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ignore
    }
  }
}

export class TaskRegistry {
  private readonly stateRoot: string;
  private readonly tasksDir: string;
  private readonly sessionId: string;
  private readonly parentSpawnId: string | null;
  private readonly bus: MeridianEventBus;
  private readonly sidecar: LifecycleSidecarWriter | null;
  private readonly spawnPingDefaults: SpawnTaskPingDefaults;
  private readonly jobs: Map<string, RuntimeTask> = new Map();
  /** Resolves an in-flight `waitForCompletion` without ending the child process. */
  private readonly waitReleases = new Map<string, () => void>();
  private pollTimer: NodeJS.Timeout | null = null;
  private pingTimer: NodeJS.Timeout | null = null;
  private readonly lastOutputEmitAt = new Map<string, number>();
  private readonly pendingOutputEmit = new Map<string, NodeJS.Timeout>();

  constructor(
    stateRoot: string,
    sessionId: string,
    parentSpawnId: string | null,
    bus: MeridianEventBus = createLocalBus(),
    sidecar: LifecycleSidecarWriter | null = null,
    spawnPingDefaults: SpawnTaskPingDefaults = resolveSpawnTaskPingDefaults(),
  ) {
    this.stateRoot = stateRoot;
    this.tasksDir = path.join(this.stateRoot, "background-tasks", sessionId, "tasks");
    this.sessionId = sessionId;
    this.parentSpawnId = parentSpawnId;
    this.bus = bus;
    this.sidecar = sidecar;
    this.spawnPingDefaults = spawnPingDefaults;
  }

  async initialize(): Promise<void> {
    await fs.mkdir(this.tasksDir, { recursive: true });
    await this.orphanScan();
    this.startPoller();
    this.startPingWorker();
  }

  private startPoller(): void {
    if (this.pollTimer != null) {
      return;
    }
    this.pollTimer = setInterval(() => {
      void this.pollForOrphanCompletions();
    }, 2_000);
    this.pollTimer.unref?.();
  }

  private stopPoller(): void {
    if (this.pollTimer == null) {
      return;
    }
    clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  private startPingWorker(): void {
    if (this.pingTimer != null) {
      return;
    }
    this.pingTimer = setInterval(() => {
      void this.scanTaskPings();
    }, PING_SCAN_INTERVAL_MS);
    this.pingTimer.unref?.();
  }

  private stopPingWorker(): void {
    if (this.pingTimer == null) {
      return;
    }
    clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private effectivePingIntervalMs(record: StoredTaskRecord): number {
    return resolveEffectivePingIntervalMs(
      record.ping_interval_ms,
      this.spawnPingDefaults.pingIntervalMs,
    );
  }

  private initializePingSchedule(record: StoredTaskRecord, atMs: number = nowMs()): void {
    record.last_activity_at_ms = atMs;
    record.next_ping_at_ms = atMs + this.effectivePingIntervalMs(record);
  }

  bumpTaskActivity(taskId: string): void {
    const runtimeJob = this.jobs.get(taskId);
    if (runtimeJob == null || runtimeJob.record.status !== "running") {
      return;
    }
    const record = runtimeJob.record;
    const atMs = nowMs();
    record.last_activity_at_ms = atMs;
    if (this.spawnPingDefaults.pingResetOnActivity) {
      record.next_ping_at_ms = atMs + this.effectivePingIntervalMs(record);
    }
    void this.persistRecord(record);
  }

  private clearTaskOutputNotify(taskId: string): void {
    const timeout = this.pendingOutputEmit.get(taskId);
    if (timeout) {
      clearTimeout(timeout);
    }
    this.pendingOutputEmit.delete(taskId);
    this.lastOutputEmitAt.delete(taskId);
  }

  private notifyTaskOutput(taskId: string): void {
    const now = nowMs();
    const lastEmit = this.lastOutputEmitAt.get(taskId) ?? 0;
    const elapsed = now - lastEmit;

    if (elapsed >= TASK_OUTPUT_THROTTLE_MS) {
      this.lastOutputEmitAt.set(taskId, now);
      this.bus.emit(TASK_OUTPUT_EVENT, { task_id: taskId });
      return;
    }

    if (!this.pendingOutputEmit.has(taskId)) {
      const delay = TASK_OUTPUT_THROTTLE_MS - elapsed;
      const timeout = setTimeout(() => {
        this.pendingOutputEmit.delete(taskId);
        if (!this.jobs.has(taskId)) {
          return;
        }
        this.lastOutputEmitAt.set(taskId, nowMs());
        this.bus.emit(TASK_OUTPUT_EVENT, { task_id: taskId });
      }, delay);
      this.pendingOutputEmit.set(taskId, timeout);
    }
  }

  private async scanTaskPings(): Promise<void> {
    const now = nowMs();
    for (const runtimeJob of this.jobs.values()) {
      const record = runtimeJob.record;
      if (record.status !== "running") {
        continue;
      }
      if (record.next_ping_at_ms == null || now < record.next_ping_at_ms) {
        continue;
      }
      this.emitTaskPing(record);
      record.next_ping_at_ms = now + this.effectivePingIntervalMs(record);
      await this.persistRecord(record);
    }
  }

  private subspawnKind(record: StoredTaskRecord): string {
    if (isMeridianSpawnCommand(record.command)) {
      return "meridian_spawn_wrapper";
    }
    return record.ingress === "bash" ? "bash" : "process";
  }

  private sidecarSubspawnKind(record: StoredTaskRecord): string {
    return record.ingress === "bash" ? "bash" : this.subspawnKind(record);
  }

  private emitSidecar(event: Record<string, unknown>): void {
    this.sidecar?.append(event);
  }

  private taskDir(taskId: string): string {
    return path.join(this.tasksDir, taskId);
  }

  private taskMetaPath(taskId: string): string {
    return path.join(this.taskDir(taskId), "meta.json");
  }

  private taskLogPath(taskId: string): string {
    return path.join(this.taskDir(taskId), "combined.log");
  }

  private async persistRecord(record: StoredTaskRecord): Promise<void> {
    const finalPath = this.taskMetaPath(record.task_id);
    const tempPath = `${finalPath}.tmp-${process.pid}-${Math.random().toString(16).slice(2)}`;
    await fs.writeFile(tempPath, `${JSON.stringify(record)}\n`, "utf-8");
    await fs.rename(tempPath, finalPath);
  }

  private createRuntimeTask(
    record: StoredTaskRecord,
    child: ChildProcess | null,
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

  private enqueueLogWrite(runtimeJob: RuntimeTask, work: () => Promise<void>): void {
    runtimeJob.logWriteChain = runtimeJob.logWriteChain
      .then(work)
      .catch(() => undefined);
  }

  private async closeLogHandle(runtimeJob: RuntimeTask): Promise<void> {
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

  private toPublicRecord(record: StoredTaskRecord): BackgroundTaskRecord {
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

  private lifecycleJobSnapshot(record: StoredTaskRecord): Record<string, unknown> {
    return {
      job_id: record.task_id,
      task_id: record.task_id,
      wait_policy: record.wait_policy,
      pid: record.pid,
      status: record.status,
      command: record.command,
      persistent: record.persistent === true,
    };
  }

  private async loadRecord(filePath: string): Promise<StoredTaskRecord | null> {
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

  private buildSubspawnEnvelope(kind: string, correlationId: string): Record<string, unknown> {
    return {
      schema_version: 1,
      session_id: this.sessionId,
      parent_spawn_id: this.parentSpawnId,
      correlation_id: correlationId,
      kind,
      emitted_at_ms: nowMs(),
    };
  }

  private emitTaskStart(record: StoredTaskRecord): void {
    const command = truncateCommand(record.command);
    const isSpawn = isMeridianSpawnCommand(record.command);
    const kind = this.subspawnKind(record);
    const envelope = this.buildSubspawnEnvelope(kind, record.task_id);
    const payload = {
      type: "meridian.task.start",
      ...envelope,
      task_id: record.task_id,
      subspawn_id: record.task_id,
      wait_policy: record.wait_policy,
      command,
      command_is_meridian_spawn: isSpawn,
      pid: record.pid,
      started_at_ms: record.started_at_ms,
      log_path: record.combined_log_path,
      label: record.label,
      persistent: record.persistent === true,
      ping_interval_ms: this.effectivePingIntervalMs(record),
    };
    this.bus.emit(TASK_START_EVENT, payload);
    this.bus.emit(SUBSPAWN_START_EVENT, payload);
    this.emitSidecar({
      type: "meridian.subspawn.start",
      ...envelope,
      kind: this.sidecarSubspawnKind(record),
      subspawn_id: record.task_id,
      wait_policy: record.wait_policy,
      command,
      command_is_meridian_spawn: isSpawn,
      pid: record.pid,
      started_at_ms: record.started_at_ms,
      log_path: record.combined_log_path,
      persistent: record.persistent === true,
    });
  }

  private emitTaskEnd(record: StoredTaskRecord): void {
    const command = truncateCommand(record.command);
    const isSpawn = isMeridianSpawnCommand(record.command);
    const kind = this.subspawnKind(record);
    const envelope = this.buildSubspawnEnvelope(kind, record.task_id);
    const payload = {
      type: "meridian.task.end",
      ...envelope,
      task_id: record.task_id,
      subspawn_id: record.task_id,
      wait_policy: record.wait_policy,
      command,
      command_is_meridian_spawn: isSpawn,
      status: record.status,
      exit_code: record.exit_code,
      success: record.success,
      signal: record.signal,
      duration_ms: record.duration_ms,
      log_path: record.combined_log_path,
      label: record.label,
      persistent: record.persistent === true,
    };
    this.bus.emit(TASK_END_EVENT, payload);
    this.bus.emit(SUBSPAWN_END_EVENT, payload);
    this.emitSidecar({
      type: "meridian.subspawn.end",
      ...envelope,
      kind: this.sidecarSubspawnKind(record),
      subspawn_id: record.task_id,
      wait_policy: record.wait_policy,
      command,
      command_is_meridian_spawn: isSpawn,
      status: record.status,
      exit_code: record.exit_code,
      success: record.success,
      signal: record.signal,
      duration_ms: record.duration_ms,
      log_path: record.combined_log_path,
      persistent: record.persistent === true,
    });
  }

  private emitTaskPing(record: StoredTaskRecord): void {
    const payload = {
      type: "meridian.task.ping",
      ...this.buildSubspawnEnvelope(this.subspawnKind(record), record.task_id),
      task_id: record.task_id,
      subspawn_id: record.task_id,
      wait_policy: record.wait_policy,
      command: truncateCommand(record.command),
      persistent: record.persistent === true,
      last_activity_at_ms: record.last_activity_at_ms,
      next_ping_at_ms: record.next_ping_at_ms,
      ping_interval_ms: this.effectivePingIntervalMs(record),
    };
    this.bus.emit(TASK_PING_EVENT, payload);
  }

  private async enforceLogCap(record: StoredTaskRecord): Promise<void> {
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

  private attachChildLifecycle(runtimeJob: RuntimeTask): void {
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

  private async finishJob(
    jobId: string,
    outcome: { exitCode: number | null; signal: string | number | null },
  ): Promise<BackgroundTaskRecord | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }
    const record = runtimeJob.record;
    if (record.status !== "running") {
      return this.toPublicRecord(record);
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

    await this.enforceLogCap(record);
    await this.persistRecord(record);
    if (record.emitted_start) {
      this.emitTaskEnd(record);
    }
    this.clearTaskOutputNotify(jobId);
    runtimeJob.resolveCompletion(record);
    return this.toPublicRecord(record);
  }

  async orphanScan(): Promise<void> {
    const entries = await fs.readdir(this.tasksDir, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      if (!entry.isDirectory()) {
        continue;
      }
      const record = await this.loadRecord(this.taskMetaPath(entry.name));
      if (record == null) {
        continue;
      }

      if (record.status !== "running") {
        const finished = this.createRuntimeTask(record, null);
        finished.resolveCompletion(record);
        this.jobs.set(record.task_id, finished);
        continue;
      }

      const runtimeJob = this.createRuntimeTask(record, null);
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
    await fs.mkdir(this.taskDir(jobId), { recursive: true });
    const logPath = this.taskLogPath(jobId);
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
    this.initializePingSchedule(record, startedAt);

    const runtimeJob = this.createRuntimeTask(record, child, logHandle);
    this.jobs.set(jobId, runtimeJob);
    await this.persistRecord(record);

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
          await this.enforceLogCap(record);
        }
        this.bumpTaskActivity(record.task_id);
        this.notifyTaskOutput(record.task_id);
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
      return this.toPublicRecord(runtimeJob.record);
    }

    runtimeJob.record.emitted_start = true;
    this.emitTaskStart(runtimeJob.record);

    if (runtimeJob.record.status !== "running") {
      this.emitTaskEnd(runtimeJob.record);
    }

    await this.persistRecord(runtimeJob.record);
    return this.toPublicRecord(runtimeJob.record);
  }

  /** Stop blocking on `waitForCompletion` while the task keeps running (e.g. `$` → background). */
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
    this.bumpTaskActivity(jobId);
    if (runtimeJob.record.status !== "running") {
      return this.toPublicRecord(runtimeJob.record);
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
      return this.toPublicRecord(done);
    } catch {
      return this.toPublicRecord(runtimeJob.record);
    } finally {
      this.waitReleases.delete(jobId);
    }
  }

  async getTask(taskId: string): Promise<BackgroundTaskRecord | null> {
    const runtimeJob = this.jobs.get(taskId);
    if (!runtimeJob) {
      return null;
    }
    return this.toPublicRecord(runtimeJob.record);
  }

  async list(includeCompleted: boolean): Promise<BackgroundTaskRecord[]> {
    const values = Array.from(this.jobs.values())
      .map((job) => this.toPublicRecord(job.record))
      .filter((record) => includeCompleted || record.status === "running")
      .sort((a, b) => b.started_at_ms - a.started_at_ms);
    return values;
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
    const waitPolicy = normalizeWaitPolicy(input.waitPolicy);
    const existing = this.jobs.get(input.taskId);
    const startedAt = nowMs();
    const cwd = input.cwd?.trim() || process.cwd();
    const logPath = input.logPath?.trim() || this.taskLogPath(input.taskId);

    if (existing == null) {
      await fs.mkdir(this.taskDir(input.taskId), { recursive: true });
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
      this.initializePingSchedule(record, startedAt);
      const runtimeJob = this.createRuntimeTask(record, null);
      this.jobs.set(input.taskId, runtimeJob);
      await this.persistRecord(record);
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
      await this.persistRecord(record);
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
      return this.toPublicRecord(runtimeJob.record);
    }
    return this.finishJob(input.taskId, {
      exitCode: input.exitCode,
      signal: input.signal,
    });
  }

  async readLog(jobId: string, maxBytes: number, offset?: number): Promise<{
    data: string;
    log_truncated: boolean;
    next_offset: number;
    eof: boolean;
  } | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }

    this.bumpTaskActivity(jobId);
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
    this.bumpTaskActivity(jobId);
    if (runtimeJob.record.status !== "running") {
      return this.toPublicRecord(runtimeJob.record);
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
    this.stopPingWorker();
    for (const taskId of this.pendingOutputEmit.keys()) {
      this.clearTaskOutputNotify(taskId);
    }
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

  async clearFinished(): Promise<number> {
    let removed = 0;
    for (const [taskId, runtimeJob] of [...this.jobs.entries()]) {
      if (runtimeJob.record.status === "running") {
        continue;
      }
      this.clearTaskOutputNotify(taskId);
      this.jobs.delete(taskId);
      removed += 1;
    }
    return removed;
  }
}


export type ToolContext = {
  cwd?: string;
  sessionManager?: { getSessionId?: () => string };
};

export function sessionIdFromContext(ctx: ToolContext | undefined, fallback: string): string {
  try {
    const sessionId = ctx?.sessionManager?.getSessionId?.();
    if (typeof sessionId === "string" && sessionId.trim()) {
      return sessionId;
    }
  } catch {
    // ignore
  }
  return fallback;
}

export function parentSpawnIdFromEnv(): string | null {
  const raw =
    process.env.MERIDIAN_PARENT_SPAWN_ID?.trim() ||
    process.env.MERIDIAN_SPAWN_ID?.trim() ||
    "";
  return raw.length > 0 ? raw : null;
}

export { clamp, toInt, makeId, normalizeWaitPolicy, isMeridianSpawnCommand, trimCombinedTails };
