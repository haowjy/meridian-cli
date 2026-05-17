import type { ChildProcess } from "node:child_process";
import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

type WaitPolicy = "tracked" | "detached";
type JobStatus = "running" | "exited" | "killed";
type InternalEventEmitter = (channel: string, payload: Record<string, unknown>) => void;

type ToolContext = {
  cwd?: string;
  sessionManager?: {
    getSessionId?: () => string;
  };
};

type JobRecord = {
  job_id: string;
  command: string;
  wait_policy: WaitPolicy;
  status: JobStatus;
  pid: number;
  started_at_ms: number;
  ended_at_ms: number | null;
  duration_ms: number | null;
  exit_code: number | null;
  signal: string | number | null;
  success: boolean | null;
  log_path: string;
  log_bytes: number;
  log_truncated: boolean;
  emitted_start: boolean;
};

type RuntimeJob = {
  record: JobRecord;
  child: ChildProcess | null;
  completion: Promise<JobRecord>;
  resolveCompletion: (value: JobRecord) => void;
  logHandle: Awaited<ReturnType<typeof fs.open>> | null;
  logHandleClosed: boolean;
  logWriteChain: Promise<void>;
};

const DEFAULT_TIMEOUT_MS = 120_000;
const MAX_COMMAND_LENGTH = 512;
const MAX_FOREGROUND_TAIL_BYTES = 16 * 1024;
const DEFAULT_BG_READ_BYTES = 8 * 1024;
const MAX_BG_READ_BYTES = 64 * 1024;
const MAX_LOG_BYTES = 10 * 1024 * 1024;
const DEFAULT_BG_WAIT_TIMEOUT_MS = 30_000;
const MAX_BG_WAIT_TIMEOUT_MS = 10 * 60 * 1000;
const INTERNAL_SUBSPAWN_START_EVENT = "meridian:subspawn:start";
const INTERNAL_SUBSPAWN_END_EVENT = "meridian:subspawn:end";
const MERIDIAN_SPAWN_COMMAND_PATTERN = /\bmeridian\s+spawn\b/;

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

function emitLifecycleEvent(event: Record<string, unknown>): void {
  try {
    process.stdout.write(`${JSON.stringify(event)}\n`);
  } catch {
    // do not throw from lifecycle emit paths
  }
}

class ManagedBashRegistry {
  private readonly stateRoot: string;
  private readonly jobsDir: string;
  private readonly sessionId: string;
  private readonly parentSpawnId: string | null;
  private readonly emitInternalEvent: InternalEventEmitter;
  private readonly jobs: Map<string, RuntimeJob> = new Map();
  private pollTimer: NodeJS.Timeout | null = null;

  constructor(
    stateRoot: string,
    sessionId: string,
    parentSpawnId: string | null,
    emitInternalEvent?: InternalEventEmitter,
  ) {
    this.stateRoot = stateRoot;
    this.jobsDir = path.join(this.stateRoot, "managed-bash", sessionId, "jobs");
    this.sessionId = sessionId;
    this.parentSpawnId = parentSpawnId;
    this.emitInternalEvent = emitInternalEvent ?? (() => undefined);
  }

  async initialize(): Promise<void> {
    await fs.mkdir(this.jobsDir, { recursive: true });
    await this.orphanScan();
    this.startPoller();
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

  private jobMetaPath(jobId: string): string {
    return path.join(this.jobsDir, `${jobId}.json`);
  }

  private jobLogPath(jobId: string): string {
    return path.join(this.jobsDir, `${jobId}.log`);
  }

  private async persistRecord(record: JobRecord): Promise<void> {
    const finalPath = this.jobMetaPath(record.job_id);
    const tempPath = `${finalPath}.tmp-${process.pid}-${Math.random().toString(16).slice(2)}`;
    await fs.writeFile(tempPath, `${JSON.stringify(record)}\n`, "utf-8");
    await fs.rename(tempPath, finalPath);
  }

  private createRuntimeJob(
    record: JobRecord,
    child: ChildProcess | null,
    logHandle: Awaited<ReturnType<typeof fs.open>> | null = null,
  ): RuntimeJob {
    let resolveCompletion!: (value: JobRecord) => void;
    const completion = new Promise<JobRecord>((resolve) => {
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

  private enqueueLogWrite(runtimeJob: RuntimeJob, work: () => Promise<void>): void {
    runtimeJob.logWriteChain = runtimeJob.logWriteChain
      .then(work)
      .catch(() => undefined);
  }

  private async closeLogHandle(runtimeJob: RuntimeJob): Promise<void> {
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

  private async loadRecord(filePath: string): Promise<JobRecord | null> {
    try {
      const raw = await fs.readFile(filePath, "utf-8");
      const parsed = JSON.parse(raw) as JobRecord;
      if (!parsed || typeof parsed !== "object") {
        return null;
      }
      if (typeof parsed.job_id !== "string") {
        return null;
      }
      if (typeof parsed.emitted_start !== "boolean") {
        parsed.emitted_start = false;
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

  private emitSubspawnStart(record: JobRecord): void {
    const command = truncateCommand(record.command);
    const payload = {
      type: "meridian.subspawn.start",
      ...this.buildSubspawnEnvelope("bash", record.job_id),
      subspawn_id: record.job_id,
      wait_policy: record.wait_policy,
      command,
      command_is_meridian_spawn: isMeridianSpawnCommand(record.command),
      pid: record.pid,
      started_at_ms: record.started_at_ms,
      log_path: record.log_path,
    };
    emitLifecycleEvent(payload);
    this.emitInternalEvent(INTERNAL_SUBSPAWN_START_EVENT, payload);
  }

  private emitSubspawnEnd(record: JobRecord): void {
    const command = truncateCommand(record.command);
    const payload = {
      type: "meridian.subspawn.end",
      ...this.buildSubspawnEnvelope("bash", record.job_id),
      subspawn_id: record.job_id,
      wait_policy: record.wait_policy,
      command,
      command_is_meridian_spawn: isMeridianSpawnCommand(record.command),
      status: record.status,
      exit_code: record.exit_code,
      success: record.success,
      signal: record.signal,
      duration_ms: record.duration_ms,
      log_path: record.log_path,
    };
    emitLifecycleEvent(payload);
    this.emitInternalEvent(INTERNAL_SUBSPAWN_END_EVENT, payload);
  }

  private async enforceLogCap(record: JobRecord): Promise<void> {
    try {
      const stat = await fs.stat(record.log_path);
      if (stat.size <= MAX_LOG_BYTES) {
        record.log_bytes = stat.size;
        await this.persistRecord(record);
        return;
      }

      const fd = await fs.open(record.log_path, "r");
      try {
        const start = stat.size - MAX_LOG_BYTES;
        const buffer = Buffer.allocUnsafe(MAX_LOG_BYTES);
        await fd.read(buffer, 0, MAX_LOG_BYTES, start);
        const tmpPath = `${record.log_path}.tmp-${process.pid}-${Math.random().toString(16).slice(2)}`;
        await fs.writeFile(tmpPath, buffer);
        await fs.rename(tmpPath, record.log_path);
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

  private attachChildLifecycle(runtimeJob: RuntimeJob): void {
    const { child, record } = runtimeJob;
    if (child == null) {
      return;
    }

    child.on("close", (exitCode, signal) => {
      void this.finishJob(record.job_id, {
        exitCode: exitCode ?? null,
        signal: signal ?? null,
      });
    });
  }

  private async finishJob(
    jobId: string,
    outcome: { exitCode: number | null; signal: string | number | null },
  ): Promise<JobRecord | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }
    const record = runtimeJob.record;
    if (record.status !== "running") {
      return record;
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
      const stat = await fs.stat(record.log_path);
      record.log_bytes = stat.size;
    } catch {
      // ignore
    }

    await this.enforceLogCap(record);
    await this.persistRecord(record);
    if (record.emitted_start) {
      this.emitSubspawnEnd(record);
    }
    runtimeJob.resolveCompletion(record);
    return record;
  }

  async orphanScan(): Promise<void> {
    const entries = await fs.readdir(this.jobsDir, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      if (!entry.isFile() || !entry.name.endsWith(".json")) {
        continue;
      }
      const record = await this.loadRecord(path.join(this.jobsDir, entry.name));
      if (record == null) {
        continue;
      }

      if (record.status !== "running") {
        const finished = this.createRuntimeJob(record, null);
        finished.resolveCompletion(record);
        this.jobs.set(record.job_id, finished);
        continue;
      }

      const runtimeJob = this.createRuntimeJob(record, null);
      this.jobs.set(record.job_id, runtimeJob);

      if (!isProcessAlive(record.pid)) {
        await this.finishJob(record.job_id, {
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
      await this.finishJob(record.job_id, {
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
  ): Promise<{ runtimeJob: RuntimeJob; stdoutTail: string[]; stderrTail: string[] }> {
    const jobId = makeId("j");
    const startedAt = nowMs();
    const logPath = this.jobLogPath(jobId);
    const logHandle = await fs.open(logPath, "a");

    const child = spawn("bash", ["-lc", command], {
      cwd,
      env,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    if (child.pid == null) {
      await logHandle.close();
      throw new Error("managed-bash: failed to start child process");
    }

    const record: JobRecord = {
      job_id: jobId,
      command,
      wait_policy: waitPolicy,
      status: "running",
      pid: child.pid,
      started_at_ms: startedAt,
      ended_at_ms: null,
      duration_ms: null,
      exit_code: null,
      signal: null,
      success: null,
      log_path: logPath,
      log_bytes: 0,
      log_truncated: false,
      emitted_start: false,
    };

    const runtimeJob = this.createRuntimeJob(record, child, logHandle);
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

  async detachJob(jobId: string): Promise<JobRecord | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }
    if (runtimeJob.record.emitted_start) {
      return runtimeJob.record;
    }

    runtimeJob.record.emitted_start = true;
    this.emitSubspawnStart(runtimeJob.record);

    if (runtimeJob.record.status !== "running") {
      this.emitSubspawnEnd(runtimeJob.record);
    }

    await this.persistRecord(runtimeJob.record);
    return runtimeJob.record;
  }

  async waitForCompletion(jobId: string, timeoutMs: number): Promise<JobRecord | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }
    if (runtimeJob.record.status !== "running") {
      return runtimeJob.record;
    }

    try {
      return await Promise.race<JobRecord>([
        runtimeJob.completion,
        delay(timeoutMs).then(() => {
          throw new Error("wait-timeout");
        }),
      ]);
    } catch {
      return runtimeJob.record;
    }
  }

  async list(includeCompleted: boolean): Promise<JobRecord[]> {
    const values = Array.from(this.jobs.values())
      .map((job) => job.record)
      .filter((record) => includeCompleted || record.status === "running")
      .sort((a, b) => b.started_at_ms - a.started_at_ms);
    return values;
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

    const record = runtimeJob.record;
    let statSize = 0;
    try {
      const stat = await fs.stat(record.log_path);
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

    const fd = await fs.open(record.log_path, "r");
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

  async killJob(jobId: string): Promise<JobRecord | null> {
    const runtimeJob = this.jobs.get(jobId);
    if (!runtimeJob) {
      return null;
    }
    if (runtimeJob.record.status !== "running") {
      return runtimeJob.record;
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
      await this.killJob(record.job_id);
    }
  }
}

function resolveStateRoot(): string {
  const explicit = process.env.MERIDIAN_PI_STATE_DIR?.trim();
  if (explicit) {
    return explicit;
  }
  const agentDir = process.env.PI_CODING_AGENT_DIR?.trim();
  if (agentDir) {
    return path.join(agentDir, ".meridian");
  }
  return path.join(process.cwd(), ".meridian");
}

function sessionIdFromContext(ctx: ToolContext | undefined, fallback: string): string {
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

function parentSpawnIdFromEnv(): string | null {
  const raw =
    process.env.MERIDIAN_PARENT_SPAWN_ID?.trim() ||
    process.env.MERIDIAN_SPAWN_ID?.trim() ||
    "";
  return raw.length > 0 ? raw : null;
}

function formatExitedResult(
  exitCode: number | null,
  signal: string | number | null,
  stdoutTail: string,
  stderrTail: string,
): Record<string, unknown> {
  const trimmed = trimCombinedTails(stdoutTail, stderrTail);
  return {
    ok: exitCode === 0,
    state: "exited",
    exit_code: exitCode,
    signal,
    stdout_tail: trimmed.stdoutTail,
    stderr_tail: trimmed.stderrTail,
    output_truncated: trimmed.outputTruncated,
  };
}

function formatRunningResult(record: JobRecord): Record<string, unknown> {
  return {
    ok: true,
    state: "running",
    job_id: record.job_id,
    wait_policy: record.wait_policy,
    pid: record.pid,
    message: `Command still running in background. Use bash_bg_read or bash_bg_wait with job_id ${record.job_id}.`,
  };
}

async function buildRegistry(pi: ExtensionAPI): Promise<{ registry: ManagedBashRegistry; sessionId: string }> {
  const sessionId = makeId("session");
  const createRegistry = (sid: string): ManagedBashRegistry =>
    new ManagedBashRegistry(
      resolveStateRoot(),
      sid,
      parentSpawnIdFromEnv(),
      (channel, payload) => {
        pi.events.emit(channel, payload);
      },
    );
  const registry = createRegistry(sessionId);

  pi.on("session_start", async (_event, ctx) => {
    const resolved = sessionIdFromContext(ctx as ToolContext, sessionId);
    const startRegistry = createRegistry(resolved);
    await startRegistry.initialize();
    await state.registry?.shutdownCleanup();
    state.registry = startRegistry;
    state.sessionId = resolved;
  });

  pi.on("session_shutdown", async () => {
    await state.registry?.shutdownCleanup();
  });

  await registry.initialize();
  return { registry, sessionId };
}

const state: {
  registry: ManagedBashRegistry | null;
  sessionId: string;
} = {
  registry: null,
  sessionId: makeId("session"),
};

export default async function managedBashExtension(pi: ExtensionAPI): Promise<void> {
  const setup = await buildRegistry(pi);
  state.registry = setup.registry;
  state.sessionId = setup.sessionId;

  pi.registerTool({
    name: "bash",
    label: "bash",
    description:
      "Run a bash command. Commands block until completion or timeout; long commands continue in background with a job handle.",
    parameters: Type.Object({
      command: Type.String({ description: "Command to execute via bash -lc" }),
      timeout_ms: Type.Optional(Type.Number({ minimum: 1 })),
      background: Type.Optional(Type.Boolean()),
      wait_policy: Type.Optional(Type.Union([Type.Literal("tracked"), Type.Literal("detached")])),
      cwd: Type.Optional(Type.String()),
      env: Type.Optional(Type.Record(Type.String(), Type.String())),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const registry = state.registry;
      if (registry == null) {
        return {
          content: [
            {
              type: "text",
              text: "Managed bash registry is unavailable.",
            },
          ],
          details: {
            ok: false,
            state: "error",
          },
          isError: true,
        };
      }

      const timeoutMs = clamp(
        toInt(params.timeout_ms, DEFAULT_TIMEOUT_MS),
        1,
        MAX_BG_WAIT_TIMEOUT_MS,
      );
      const background = params.background === true;
      const waitPolicy = normalizeWaitPolicy(params.wait_policy);

      const sessionId = sessionIdFromContext(ctx as ToolContext, state.sessionId);
      if (sessionId !== state.sessionId) {
        // session changed after initial setup; rebuild lazily.
        state.registry = new ManagedBashRegistry(
          resolveStateRoot(),
          sessionId,
          parentSpawnIdFromEnv(),
          (channel, payload) => {
            pi.events.emit(channel, payload);
          },
        );
        await state.registry.initialize();
      }

      const activeRegistry = state.registry;
      if (activeRegistry == null) {
        throw new Error("managed bash registry unavailable");
      }

      const command = String(params.command ?? "");
      const cwd = typeof params.cwd === "string" && params.cwd.length > 0 ? params.cwd : (ctx as ToolContext).cwd ?? process.cwd();
      const env = {
        ...process.env,
        ...(typeof params.env === "object" && params.env ? params.env : {}),
      } as Record<string, string>;

      const { runtimeJob, stdoutTail, stderrTail } = await activeRegistry.startJob(
        command,
        waitPolicy,
        cwd,
        env,
      );

      if (background) {
        await activeRegistry.detachJob(runtimeJob.record.job_id);
        return {
          content: [
            {
              type: "text",
              text: `Started background job ${runtimeJob.record.job_id}.`,
            },
          ],
          details: formatRunningResult(runtimeJob.record),
        };
      }

      type ForegroundWaitOutcome =
        | { kind: "completed"; record: JobRecord }
        | { kind: "timeout" }
        | { kind: "aborted" };
      const completion = await Promise.race<ForegroundWaitOutcome>([
        runtimeJob.completion.then((record) => ({ kind: "completed", record })),
        delay(timeoutMs, null, { signal })
          .then(() => ({ kind: "timeout" as const }))
          .catch((error: unknown) => {
            if (signal.aborted || (error instanceof Error && error.name === "AbortError")) {
              return { kind: "aborted" as const };
            }
            return { kind: "timeout" as const };
          }),
      ]);

      if (completion.kind === "completed" && completion.record.status !== "running") {
        const stdout = stdoutTail.join("");
        const stderr = stderrTail.join("");
        const details = formatExitedResult(
          completion.record.exit_code,
          completion.record.signal,
          stdout,
          stderr,
        );
        return {
          content: [
            {
              type: "text",
              text: `Command finished with exit code ${completion.record.exit_code ?? "null"}.`,
            },
          ],
          details,
          isError: completion.record.exit_code !== 0,
        };
      }

      if (completion.kind === "aborted") {
        const record = await activeRegistry.killJob(runtimeJob.record.job_id);
        return {
          content: [
            {
              type: "text",
              text: `Command cancelled. Terminated job ${runtimeJob.record.job_id}.`,
            },
          ],
          details: {
            ok: false,
            state: "cancelled",
            job_id: runtimeJob.record.job_id,
            job: record,
          },
          isError: true,
        };
      }

      await activeRegistry.detachJob(runtimeJob.record.job_id);
      return {
        content: [
          {
            type: "text",
            text: `Command exceeded timeout and continues in background as ${runtimeJob.record.job_id}.`,
          },
        ],
        details: formatRunningResult(runtimeJob.record),
      };
    },
  });

  pi.registerTool({
    name: "bash_bg_list",
    label: "bash_bg_list",
    description: "List managed background bash jobs for this Pi session.",
    parameters: Type.Object({
      include_completed: Type.Optional(Type.Boolean()),
    }),
    async execute(_toolCallId, params) {
      const registry = state.registry;
      if (registry == null) {
        return {
          content: [{ type: "text", text: "Managed bash registry unavailable." }],
          details: { jobs: [] },
        };
      }

      const includeCompleted = params.include_completed === true;
      const jobs = await registry.list(includeCompleted);
      return {
        content: [{ type: "text", text: `Found ${jobs.length} job(s).` }],
        details: { jobs },
      };
    },
  });

  pi.registerTool({
    name: "bash_bg_read",
    label: "bash_bg_read",
    description: "Read managed background job log output.",
    parameters: Type.Object({
      job_id: Type.String(),
      max_bytes: Type.Optional(Type.Number({ minimum: 1, maximum: MAX_BG_READ_BYTES })),
      offset: Type.Optional(Type.Number({ minimum: 0 })),
    }),
    async execute(_toolCallId, params) {
      const registry = state.registry;
      if (registry == null) {
        return {
          content: [{ type: "text", text: "Managed bash registry unavailable." }],
          details: { found: false },
        };
      }

      const maxBytes = clamp(
        toInt(params.max_bytes, DEFAULT_BG_READ_BYTES),
        1,
        MAX_BG_READ_BYTES,
      );
      const offset = typeof params.offset === "number" ? Math.max(0, Math.trunc(params.offset)) : undefined;
      const result = await registry.readLog(params.job_id, maxBytes, offset);
      if (result == null) {
        return {
          content: [{ type: "text", text: `Job ${params.job_id} not found.` }],
          details: { found: false },
          isError: true,
        };
      }

      return {
        content: [{ type: "text", text: result.data }],
        details: {
          found: true,
          ...result,
        },
      };
    },
  });

  pi.registerTool({
    name: "bash_bg_wait",
    label: "bash_bg_wait",
    description: "Wait for a managed background job to finish.",
    parameters: Type.Object({
      job_id: Type.String(),
      timeout_ms: Type.Optional(Type.Number({ minimum: 1, maximum: MAX_BG_WAIT_TIMEOUT_MS })),
    }),
    async execute(_toolCallId, params) {
      const registry = state.registry;
      if (registry == null) {
        return {
          content: [{ type: "text", text: "Managed bash registry unavailable." }],
          details: { found: false },
        };
      }

      const timeoutMs = clamp(
        toInt(params.timeout_ms, DEFAULT_BG_WAIT_TIMEOUT_MS),
        1,
        MAX_BG_WAIT_TIMEOUT_MS,
      );
      const record = await registry.waitForCompletion(params.job_id, timeoutMs);
      if (record == null) {
        return {
          content: [{ type: "text", text: `Job ${params.job_id} not found.` }],
          details: { found: false },
          isError: true,
        };
      }

      if (record.status === "running") {
        return {
          content: [{ type: "text", text: `Job ${params.job_id} is still running.` }],
          details: {
            found: true,
            state: "running",
            job: record,
          },
        };
      }

      const log = await registry.readLog(record.job_id, DEFAULT_BG_READ_BYTES);
      return {
        content: [{ type: "text", text: log?.data ?? "" }],
        details: {
          found: true,
          state: "exited",
          job: record,
          log_tail: log?.data ?? "",
          log_truncated: log?.log_truncated ?? false,
        },
        isError: record.exit_code !== 0,
      };
    },
  });

  pi.registerTool({
    name: "bash_bg_kill",
    label: "bash_bg_kill",
    description: "Kill a managed background job.",
    parameters: Type.Object({
      job_id: Type.String(),
    }),
    async execute(_toolCallId, params) {
      const registry = state.registry;
      if (registry == null) {
        return {
          content: [{ type: "text", text: "Managed bash registry unavailable." }],
          details: { found: false },
        };
      }

      const record = await registry.killJob(params.job_id);
      if (record == null) {
        return {
          content: [{ type: "text", text: `Job ${params.job_id} not found.` }],
          details: { found: false },
          isError: true,
        };
      }

      return {
        content: [{ type: "text", text: `Job ${params.job_id} terminated.` }],
        details: {
          found: true,
          job: record,
        },
      };
    },
  });
}
