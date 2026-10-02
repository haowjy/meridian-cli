import { readFile } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import { StringDecoder } from "node:string_decoder";

import { classifyWorkId } from "../../shared/ids";
import { writeJsonAtomic } from "../../shared/json_file";
import { runMeridianCommand } from "../../shared/meridian_cli";
import { admittedWorkIds, readDeliveryReceipts } from "../../shared/delivery_receipts";
import {
  currentSpawnIdFromEnv,
  resolveBashLogsDir,
  resolveBashRecordsPath,
  resolveDeliveryReceiptsPath,
} from "../../shared/pi_state_paths";
import { isTerminalBashStatus, parseBashRecordsFile, type BashRecord, type BashRecordsFile, type BashStatus } from "../../shared/schemas";
import { BashLogStore, type BashLogPaths } from "./bash_log_store";
import { ShellTask } from "./shell_task";

export type BashParams = {
  command: string;
  timeout_min?: number;
  background?: boolean;
};

export type BashManageParams = {
  action: "list" | "output" | "kill" | "wait" | "detach";
  bash_id?: string;
  include_completed?: boolean;
  timeout_min?: number;
};

export type UserBashExecOptions = {
  onData?: (data: Buffer) => void;
  signal?: AbortSignal;
  env?: NodeJS.ProcessEnv;
};

type RuntimeRecord = BashRecord & {
  task: ShellTask | null;
  outputQueue: Promise<void>;
  finished: Promise<void>;
  resolveFinished: () => void;
  rejectFinished: (error: Error) => void;
  terminationReason: BashStatus | null;
  foregroundFinish: ((result: unknown) => void) | null;
  pingTimer: NodeJS.Timeout | null;
};

export type BashRuntimeHooks = {
  onForegroundStart?: (bashId: string) => void;
  onForegroundStop?: (bashId: string) => void;
  onBackgroundPing?: (record: BashRecord) => void | Promise<void>;
};

export type BashListRow = BashRecord & {
  type: "bash";
  duration_secs: number;
};

export type BashOutputResult = {
  bash_id: string;
  output: string;
  truncated: boolean;
};

export type BashKillResult = {
  bash_id: string;
  killed: boolean;
  message: string;
};

export type BashWaitResult = {
  bash_id: string;
  status: string;
  exit_code?: number | null;
  duration_secs?: number;
  output?: string;
  message?: string;
};

export type BashDetachResult = {
  bash_id: string;
  detached: boolean;
  message: string;
};

export type BashManageResult =
  | { rows: BashListRow[] }
  | BashOutputResult
  | BashKillResult
  | BashWaitResult
  | BashDetachResult
  | { error: string };

type ExecResult = {
  stdout: string;
  stderr: string;
  exit_code: number;
};

const DEFAULT_TIMEOUT_MIN = 55;
const DEFAULT_WAIT_TIMEOUT_MIN = 10;
const MAX_TIMEOUT_MIN = 59;
const LOG_TAIL_BYTES = 4 * 1024;
const DEFAULT_TASK_PING_INTERVAL_MS = 55 * 60_000;
const TASK_PING_INTERVAL_ENV = "_MERIDIAN_PI_TASK_PING_INTERVAL_MS";
const TASK_PING_RESET_ON_ACTIVITY_ENV = "_MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY";
export const USER_BASH_PANEL_BACKGROUND_MSG = "Sent to background — /ps";

const ownersKey = Symbol.for("meridian.pi.managed-bash.owners.v2");
const scope = globalThis as typeof globalThis & { [ownersKey]?: Map<string, BashRuntime> };
const owners = scope[ownersKey] ??= new Map<string, BashRuntime>();

export class BashRuntime {
  private readonly spawnId = currentSpawnIdFromEnv();
  private readonly recordsPath = resolveBashRecordsPath(this.spawnId);
  private readonly receiptsPath = resolveDeliveryReceiptsPath(this.spawnId);
  private readonly logStore = new BashLogStore(resolveBashLogsDir(this.spawnId));
  private readonly records = new Map<string, RuntimeRecord>();
  private persistQueue: Promise<void> = Promise.resolve();
  private ready: Promise<void> = Promise.resolve();
  private runtimeError: string | undefined;

  constructor(private hooks: BashRuntimeHooks = {}) {
    const key = this.recordsPath;
    const prior = owners.get(key);
    if (prior) {
      prior.hooks = hooks;
      return prior;
    }
    owners.set(key, this);
    this.ready = this.recoverRecords();
    // Construction cannot await recovery; commands observe its rejection.
    void this.ready.catch(() => undefined);
  }

  async execute(params: BashParams, signal: AbortSignal | undefined): Promise<unknown> {
    const timeoutMin = normalizeTimeoutMin(params.timeout_min, DEFAULT_TIMEOUT_MIN);
    const record = await this.startRecord(params.command, timeoutMin);

    if (params.background === true) {
      record.is_background = true;
      await this.publishRecord(record);
      this.schedulePing(record);
      return { bash_id: record.bash_id, status: "started" };
    }

    this.hooks.onForegroundStart?.(record.bash_id);
    return await new Promise<unknown>((resolve) => {
      let settled = false;
      const finish = (result: unknown): void => {
        if (settled) return;
        settled = true;
        record.foregroundFinish = null;
        clearTimeout(timeout);
        signal?.removeEventListener("abort", abort);
        this.hooks.onForegroundStop?.(record.bash_id);
        resolve(result);
      };
      record.foregroundFinish = finish;
      const abort = (): void => {
        void this.killBash(record.bash_id, "aborted").then(async () =>
          finish({ stdout: await this.readLog(record, LOG_TAIL_BYTES), stderr: "[command aborted]", exit_code: -1 }))
          .catch((error) => finish({ error: errorText(error) }));
      };
      const timeout = setTimeout(() => {
        record.is_background = true;
        void this.persist().then(() => {
          this.schedulePing(record);
          finish({
          bash_id: record.bash_id,
          status: "backgrounded",
          message: `Command exceeded timeout_min=${timeoutMin} and was backgrounded as ${record.bash_id}. Use /ps to manage it.`,
          });
        }, async (error) => { await this.failRecord(record, error); finish({ error: errorText(error) }); });
      }, timeoutMin * 60_000);

      if (signal?.aborted) {
        abort();
        return;
      }
      signal?.addEventListener("abort", abort, { once: true });
      void record.finished.then(async () => {
        if (settled) return;
        const output = await this.readSplitLog(record);
        finish(output);
      }).catch((error) => finish({ error: errorText(error) }));
    });
  }

  async executeUserBash(
    command: string,
    cwd: string,
    options: UserBashExecOptions = {},
  ): Promise<{ exitCode: number | null }> {
    let forwardForegroundOutput = true;
    const forwardOutput = (data: Buffer): void => {
      if (forwardForegroundOutput) safeOnData(options.onData, data);
    };
    const record = await this.startRecord(command, DEFAULT_TIMEOUT_MIN, cwd, { ...process.env, ...(options.env ?? {}) }, forwardOutput);
    this.hooks.onForegroundStart?.(record.bash_id);

    return await new Promise<{ exitCode: number | null }>((resolve) => {
      let settled = false;
      const finish = (exitCode: number | null): void => {
        if (settled) return;
        settled = true;
        forwardForegroundOutput = false;
        record.foregroundFinish = null;
        options.signal?.removeEventListener("abort", abort);
        this.hooks.onForegroundStop?.(record.bash_id);
        resolve({ exitCode });
      };
      const abort = (): void => {
        forwardForegroundOutput = false;
        void this.killBash(record.bash_id, "aborted").then(() => finish(-1), () => finish(-1));
      };
      record.foregroundFinish = () => {
        safeOnData(options.onData, Buffer.from(`${USER_BASH_PANEL_BACKGROUND_MSG}\n`, "utf-8"));
        finish(0);
      };

      if (options.signal?.aborted) {
        abort();
        return;
      }
      options.signal?.addEventListener("abort", abort, { once: true });
      void record.finished.then(() => finish(record.exit_code), () => finish(-1));
    });
  }

  async startDetachedUserBash(command: string, cwd: string, env: NodeJS.ProcessEnv = process.env): Promise<{ bash_id: string }> {
    const record = await this.startRecord(command, DEFAULT_TIMEOUT_MIN, cwd, env);
    record.is_background = true;
    await this.publishRecord(record);
    this.schedulePing(record);
    return { bash_id: record.bash_id };
  }

  async backgroundForeground(): Promise<{ ok: boolean; reason?: string; bash_id?: string }> {
    const foreground = [...this.records.values()]
      .filter((record) => record.status === "running" && !record.is_background && record.foregroundFinish != null)
      .sort((a, b) => b.started_at_ms - a.started_at_ms)[0];
    if (!foreground) {
      return { ok: false, reason: "no_foreground" };
    }

    foreground.is_background = true;
    await this.publishRecord(foreground);
    this.schedulePing(foreground);
    foreground.foregroundFinish?.({
      bash_id: foreground.bash_id,
      status: "backgrounded",
      message: USER_BASH_PANEL_BACKGROUND_MSG,
    });
    return { ok: true, bash_id: foreground.bash_id };
  }

  list(includeCompleted: boolean): BashListRow[] {
    return [...this.records.values()]
      .filter((record) => includeCompleted || record.status === "running")
      .sort(compareBashRecordsForPanel)
      .map((record) => ({
        type: "bash" as const,
        bash_id: record.bash_id,
        command: record.command,
        cwd: record.cwd,
        pid: record.pid,
        status: record.status,
        is_background: record.is_background,
        is_tracked: record.is_tracked,
        started_at_ms: record.started_at_ms,
        ended_at_ms: record.ended_at_ms,
        exit_code: record.exit_code,
        duration_secs: durationSecs(record),
        log_path: record.log_path,
        stdout_log_path: record.stdout_log_path,
        stderr_log_path: record.stderr_log_path,
        log_bytes: record.log_bytes,
        timeout_min: record.timeout_min,
        originating_bash_id: record.originating_bash_id,
        execution_error: record.execution_error,
      }));
  }

  async rows(): Promise<BashListRow[]> {
    await this.ensureReady();
    return this.list(true);
  }

  async clearFinished(): Promise<number> {
    await this.prepare();
    const admitted = admittedWorkIds(await readDeliveryReceipts(this.spawnId, this.receiptsPath));
    const finished = [...this.records.values()].filter((record) => record.status !== "running" && (!record.is_background || !record.is_tracked || record.notification_consumed_at_ms != null || admitted.has(record.bash_id)));
    for (const record of finished) this.records.delete(record.bash_id);
    if (finished.length > 0) {
      try { await this.persist(); }
      catch (error) { for (const record of finished) this.records.set(record.bash_id, record); throw error; }
    }
    return finished.length;
  }

  async manage(params: BashManageParams): Promise<BashManageResult> {
    await this.prepare();
    const action = params.action;
    if (action === "list") {
      return { rows: this.list(params.include_completed === true) };
    }

    const id = params.bash_id?.trim();
    if (!id) {
      return { error: `bash_id is required for action '${action}'` };
    }

    const kind = classifyWorkId(id);
    if (kind === "spawn") {
      return await this.manageSpawn(id, params);
    }
    if (kind !== "bash") {
      return { error: `unsupported id: ${id}` };
    }

    const record = this.records.get(id);
    if (!record) {
      return { error: `bash_id not found: ${id}` };
    }

    switch (action) {
      case "output":
        return { bash_id: id, output: await this.readLog(record, LOG_TAIL_BYTES), truncated: true };
      case "kill":
        return await this.killBash(id, "killed");
      case "wait": {
        const result = await this.waitBash(record, normalizeTimeoutMin(params.timeout_min, DEFAULT_WAIT_TIMEOUT_MIN));
        if (isTerminalBashStatus(result.status)) {
          await this.persistWaitConsumption(record);
        }
        return result;
      }
      case "detach":
        const wasTracked = record.is_tracked;
        record.is_tracked = false;
        this.clearPing(record);
        try { await this.persist(); }
        catch (error) { record.is_tracked = wasTracked; this.schedulePing(record); throw error; }
        return { bash_id: id, detached: true, message: `${id} detached from quiescence tracking.` };
      default:
        return { error: `unsupported action: ${String(action)}` };
    }
  }

  async shutdown(): Promise<void> {
    await this.ensureReady();
    const results = await Promise.allSettled([...this.records.values()].map(async (record) => {
      this.clearPing(record);
      if (record.task && record.status === "running") await this.killBash(record.bash_id, "killed");
    }));
    await this.persist();
    const failure = results.find((result) => result.status === "rejected");
    if (failure?.status === "rejected") throw failure.reason;
  }

  private async startRecord(
    command: string,
    timeoutMin: number,
    cwd = process.cwd(),
    env: NodeJS.ProcessEnv = process.env,
    onData?: (data: Buffer) => void,
  ): Promise<RuntimeRecord> {
    await this.prepare();
    const bashId = makeBashId();
    const logPaths = await this.logStore.create(bashId);

    const record = runtimeRecord({
      bash_id: bashId,
      command,
      cwd,
      pid: null,
      status: "running",
      is_background: false,
      is_tracked: true,
      exit_code: null,
      started_at_ms: Date.now(),
      ended_at_ms: null,
      log_path: logPaths.combined,
      stdout_log_path: logPaths.stdout,
      stderr_log_path: logPaths.stderr,
      log_bytes: 0,
      timeout_min: timeoutMin,
      originating_bash_id: process.env._MERIDIAN_PI_BASH_ID || null,
      ping_sent_at_ms: null,
    });
    this.records.set(bashId, record);

    let task: ShellTask;
    try { task = new ShellTask(command, cwd, { ...env, _MERIDIAN_PI_BASH_ID: bashId }); }
    catch (error) { this.records.delete(bashId); throw error; }
    const child = task.child;
    record.task = task;
    record.pid = child.pid ?? null;
    this.attachOutput(record, task, onData);
    child.once("error", (error) => this.queueOutput(record, `\n[failed to start command: ${error.message}]\n`, "stderr"));
    // Exit observation waits for the owned group and every preceding log write.
    void task.waitForExit().then(async (code) => {
      await record.outputQueue;
      if (record.status === "running") await this.markTerminal(record, record.terminationReason ?? "exited", code);
    }).catch((error) => this.failRecord(record, error));

    try {
      await this.persist();
    } catch (error) {
      await this.failRecord(record, error);
      throw error;
    }
    return record;
  }

  private attachOutput(record: RuntimeRecord, task: ShellTask, onData?: (data: Buffer) => void): void {
    const child = task.child;
    for (const stream of ["stdout", "stderr"] as const) {
      const output = child[stream];
      const decoder = new StringDecoder("utf-8");
      output?.on("error", (error) => { void this.failRecord(record, error); });
      output?.on("data", (chunk: string | Buffer) => {
        output.pause();
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, "utf-8");
        try { onData?.(buffer); }
        catch (error) { void this.failRecord(record, error); }
        void this.queueOutput(record, decoder.write(buffer), stream).then(() => output.resume());
      });
      output?.once("end", () => { const tail = decoder.end(); if (tail) void this.queueOutput(record, tail, stream); });
    }
  }

  private queueOutput(record: RuntimeRecord, chunk: string, stream: "stdout" | "stderr"): Promise<void> {
    record.outputQueue = record.outputQueue.then(async () => {
      if (!record.execution_error) await this.appendLog(record, chunk, stream);
    }).catch((error) => this.failRecord(record, error));
    return record.outputQueue;
  }

  private async appendLog(record: RuntimeRecord, chunk: string, stream: "stdout" | "stderr"): Promise<void> {
    const size = await this.logStore.append(logPathsFromRecord(record), stream, chunk);
    record.log_bytes = size;
    if (shouldResetPingOnActivity()) this.schedulePing(record);
    await this.persist();
  }

  private async markTerminal(
    record: RuntimeRecord,
    status: BashStatus,
    exitCode: number | null,
  ): Promise<void> {
    record.status = status;
    record.exit_code = exitCode;
    record.ended_at_ms = Date.now();
    record.task = null;
    this.clearPing(record);
    await this.persist();
    record.resolveFinished();
  }

  private async killBash(bashId: string, reason: "killed" | "aborted"): Promise<BashKillResult> {
    const record = this.records.get(bashId);
    if (!record) {
      return { bash_id: bashId, killed: false, message: `bash_id not found: ${bashId}` };
    }
    if (record.status !== "running") {
      return { bash_id: bashId, killed: false, message: `${bashId} is already ${record.status}` };
    }
    if (!record.task) {
      return { bash_id: bashId, killed: false, message: `${bashId}: ownership lost after restart; no PID was signalled. Inspect the process manually or use bash_manage detach to release tracking.` };
    }
    record.terminationReason = "killed";
    try { await record.task.terminate(); }
    catch (error) { await this.failRecord(record, error); throw error; }
    await record.finished;
    return { bash_id: bashId, killed: true, message: `${bashId} killed` };
  }

  private async waitBash(record: RuntimeRecord, timeoutMin: number): Promise<BashWaitResult> {
    if (record.status === "running" && !record.task) throw new Error(`${record.bash_id}: ownership_lost after restart; inspect manually or detach to release tracking`);
    if (record.status === "running") {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, timeoutMin * 60_000);
        void record.finished.then(() => {
          clearTimeout(timer);
          resolve();
        }, () => { clearTimeout(timer); resolve(); });
      });
    }
    if (record.execution_error) throw new Error(record.execution_error);
    if (record.status === "running") {
      return {
        bash_id: record.bash_id,
        status: "running",
        message: `Still running after timeout_min=${timeoutMin}. Use bash_manage(action='wait') again or bash_manage(action='kill') to terminate.`,
      };
    }
    return {
      bash_id: record.bash_id,
      status: record.status,
      exit_code: record.exit_code,
      duration_secs: durationSecs(record),
      output: (await this.readLog(record, 2 * 1024)) + (record.execution_error ? `\n[execution failed: ${record.execution_error}]` : ""),
    };
  }

  private schedulePing(record: RuntimeRecord): void {
    this.clearPing(record);
    if (
      record.status !== "running" ||
      !record.is_background ||
      !record.is_tracked ||
      record.ping_sent_at_ms != null
    ) {
      return;
    }
    const timer = setTimeout(() => { void this.firePing(record).catch((error) => this.failRecord(record, error)); }, taskPingIntervalMs());
    timer.unref();
    record.pingTimer = timer;
  }

  private clearPing(record: RuntimeRecord): void {
    if (record.pingTimer) clearTimeout(record.pingTimer);
    record.pingTimer = null;
  }

  private async firePing(record: RuntimeRecord): Promise<void> {
    record.pingTimer = null;
    if (
      record.status !== "running" ||
      !record.is_background ||
      !record.is_tracked ||
      record.ping_sent_at_ms != null
    ) {
      return;
    }
    record.ping_sent_at_ms = Date.now();
    await this.persist();
    await this.hooks.onBackgroundPing?.(toPlainRecord(record));
  }

  private async manageSpawn(spawnId: string, params: BashManageParams): Promise<BashManageResult> {
    switch (params.action) {
      case "output": {
        const result = await runMeridianCommand(["session", "log", spawnId, "--tail", "20"], 15_000);
        return { bash_id: spawnId, output: result.stdout || result.stderr, truncated: false };
      }
      case "kill": {
        const result = await runMeridianCommand(["spawn", "cancel", spawnId], 15_000);
        return { bash_id: spawnId, killed: result.exitCode === 0, message: result.stdout || result.stderr };
      }
      case "wait": {
        const timeout = String(normalizeTimeoutMin(params.timeout_min, DEFAULT_WAIT_TIMEOUT_MIN));
        const result = await runMeridianCommand(["spawn", "wait", spawnId, "--timeout", timeout], (Number(timeout) * 60 + 5) * 1000);
        return { bash_id: spawnId, status: result.exitCode === 0 ? "exited" : "running", output: result.stdout || result.stderr };
      }
      case "detach":
        return { bash_id: spawnId, detached: false, message: "detach only applies to b-* bash records" };
      default:
        return { error: `unsupported p-* action: ${params.action}` };
    }
  }

  private async readSplitLog(record: RuntimeRecord): Promise<ExecResult> {
    return {
      stdout: await this.readLog(record, LOG_TAIL_BYTES, "stdout"),
      stderr: await this.readLog(record, LOG_TAIL_BYTES, "stderr"),
      exit_code: record.exit_code ?? -1,
    };
  }

  private async readLog(record: RuntimeRecord, maxBytes: number, stream: "combined" | "stdout" | "stderr" = "combined"): Promise<string> {
    return this.logStore.read(logPathsFromRecord(record), stream, maxBytes);
  }

  private persist(): Promise<void> {
    return this.enqueuePersist(() => this.writeCurrentRecords());
  }

  private async publishRecord(record: RuntimeRecord): Promise<void> {
    try { await this.persist(); }
    catch (error) { await this.failRecord(record, error); throw error; }
  }

  private async prepare(): Promise<void> {
    await this.ensureReady();
    if (this.runtimeError) {
      const previous = this.runtimeError;
      this.runtimeError = undefined;
      try { await this.persist(); }
      catch (error) { this.runtimeError = previous; throw error; }
    }
  }

  private async ensureReady(): Promise<void> {
    try { await this.ready; }
    catch {
      // An operator can repair unreadable cold-start evidence without needing
      // to abandon the process. Never replace it with an empty map.
      this.ready = this.recoverRecords();
      await this.ready;
    }
  }

  private async recoverRecords(): Promise<void> {
    let text: string;
    try { text = await readFile(this.recordsPath, "utf-8"); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
      throw error;
    }
    const file = parseBashRecordsFile(JSON.parse(text));
    if (!file || file.spawn_id !== this.spawnId) throw new Error("Invalid managed Bash record store; refusing to replace recovery evidence");
    this.runtimeError = file.runtime_error;
    for (const plain of Object.values(file.records)) {
      const record = runtimeRecord(plain);
      if (record.status === "running") {
        record.execution_error = "ownership_lost: previous runtime exited; inspect manually or use bash_manage detach to release tracking";
      }
      this.records.set(record.bash_id, record);
    }
    // A recovered running row is unresolved work, not permission to signal a
    // possibly reused PID. Publish its diagnostic before accepting commands.
    await this.persist();
  }

  private async failRecord(record: RuntimeRecord, error: unknown): Promise<void> {
    if (record.execution_error) return;
    record.execution_error = errorText(error);
    record.terminationReason = "killed";
    this.runtimeError = record.execution_error;
    this.clearPing(record);
    // No further output can be published. Closing owned pipes avoids tying
    // cleanup to paused read buffers or a failed output queue.
    record.task?.child.stdout?.destroy();
    record.task?.child.stderr?.destroy();
    try {
      if (record.task) await record.task.terminate();
      record.task = null;
      record.status = "killed";
      record.exit_code = -1;
      record.ended_at_ms = Date.now();
    } catch (cleanupError) {
      record.execution_error += `; cleanup failed: ${errorText(cleanupError)}`;
    }
    try { await this.persist(); }
    catch (publishError) {
      process.stderr.write(`[managed-bash] task ${record.bash_id}: ${errorText(publishError)}\n`);
    }
    record.rejectFinished(new Error(record.execution_error));
  }

  private persistWaitConsumption(record: RuntimeRecord): Promise<void> {
    return this.enqueuePersist(async () => {
      const previous = record.notification_consumed_at_ms;
      record.notification_consumed_at_ms = Date.now();
      try {
        await this.writeCurrentRecords();
      } catch (error) {
        record.notification_consumed_at_ms = previous;
        throw error;
      }
    });
  }

  private enqueuePersist(writeCurrentRecords: () => Promise<void>): Promise<void> {
    const write = this.persistQueue.catch(() => undefined).then(writeCurrentRecords);
    this.persistQueue = write;
    return write;
  }

  private async writeCurrentRecords(): Promise<void> {
    const records: Record<string, BashRecord> = {};
    for (const [id, record] of this.records.entries()) {
      records[id] = toPlainRecord(record);
    }
    const file: BashRecordsFile = {
      v: 1,
      spawn_id: this.spawnId,
      updated_at_ms: Date.now(),
      records,
      ...(this.runtimeError ? { runtime_error: this.runtimeError } : {}),
    };
    await writeJsonAtomic(this.recordsPath, file);
  }
}

function taskPingIntervalMs(): number {
  const raw = Number.parseInt(process.env[TASK_PING_INTERVAL_ENV] ?? "", 10);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_TASK_PING_INTERVAL_MS;
}

function shouldResetPingOnActivity(): boolean {
  return (process.env[TASK_PING_RESET_ON_ACTIVITY_ENV] ?? "true").toLowerCase() !== "false";
}

function toPlainRecord(record: RuntimeRecord): BashRecord {
  const { task: _task, outputQueue: _outputQueue, finished: _finished, resolveFinished: _resolve,
    rejectFinished: _reject, terminationReason: _reason,
    foregroundFinish: _foregroundFinish, pingTimer: _pingTimer, ...plain } = record;
  return plain;
}

function runtimeRecord(plain: BashRecord): RuntimeRecord {
  let resolveFinished!: () => void;
  let rejectFinished!: (error: Error) => void;
  const finished = new Promise<void>((resolve, reject) => { resolveFinished = resolve; rejectFinished = reject; });
  void finished.catch(() => undefined);
  if (plain.status !== "running") resolveFinished();
  return { ...plain, task: null, outputQueue: Promise.resolve(), finished,
    resolveFinished, rejectFinished, terminationReason: null,
    foregroundFinish: null, pingTimer: null };
}

function errorText(error: unknown): string { return error instanceof Error ? error.message : String(error); }

function makeBashId(): string {
  return `b-${randomBytes(4).toString("hex")}`;
}

function normalizeTimeoutMin(value: number | undefined, fallback: number): number {
  if (value == null || !Number.isFinite(value)) return fallback;
  if (value < 1 || value > MAX_TIMEOUT_MIN) {
    throw new Error(`timeout_min must be between 1 and ${MAX_TIMEOUT_MIN}`);
  }
  return Math.floor(value);
}

function safeOnData(onData: ((data: Buffer) => void) | undefined, data: Buffer): void {
  try {
    onData?.(data);
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ERR_STREAM_WRITE_AFTER_END") {
      return;
    }
    throw error;
  }
}

function compareBashRecordsForPanel(a: BashRecord, b: BashRecord): number {
  const statusRank = (record: BashRecord): number => (record.status === "running" ? 0 : 1);
  const rankDelta = statusRank(a) - statusRank(b);
  if (rankDelta !== 0) return rankDelta;
  return b.started_at_ms - a.started_at_ms;
}

function durationSecs(record: BashRecord): number {
  return Math.max(0, ((record.ended_at_ms ?? Date.now()) - record.started_at_ms) / 1000);
}

function logPathsFromRecord(record: BashRecord): BashLogPaths {
  return {
    combined: record.log_path,
    stdout: record.stdout_log_path,
    stderr: record.stderr_log_path,
  };
}
