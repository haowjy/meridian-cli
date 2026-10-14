import { readFile, rm } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import { StringDecoder } from "node:string_decoder";

import { classifyWorkId } from "../../shared/ids";
import { runMeridianCommand } from "../../shared/meridian_cli";
import { admittedWorkIds, readDeliveryReceipts } from "../../shared/delivery_receipts";
import {
  currentRunIdFromEnv,
  resolveBashLogsDir,
  resolveBashRecordsPath,
  resolveDeliveryReceiptsPath,
} from "../../shared/pi_state_paths";
import { isTerminalBashStatus, parseBashRecordsFile, type BashRecord, type BashStatus } from "../../shared/schemas";
import { bashOutcome, type BashOutcome, type TaskOutcome } from "../../shared/task_outcome";
import { BashLogStore, type BashLogPaths, type LogReadResult } from "./bash_log_store";
import { BashPublication, changeRow, withoutRows, withRow, withRuntimeError, type Published } from "./bash_publication";
import { ShellTask } from "./shell_task";
import { waitForSpawn } from "./spawn_wait";

export type BashParams = {
  command: string;
  timeout_min?: number;
  background?: boolean;
};

export type BashManageParams = {
  action: "list" | "output" | "kill" | "wait" | "detach";
  bash_id?: string;
  include_completed?: boolean;
};

export type UserBashExecOptions = {
  onData?: (data: Buffer) => void;
  signal?: AbortSignal;
  env?: NodeJS.ProcessEnv;
};

export type BashExecuteResult =
  | { kind: "start"; bash_id: string; status: "started" }
  | { kind: "start"; bash_id: string; status: "backgrounded"; message: string }
  | BashForegroundResult
  | { kind: "error"; error: string };

/** Live state of one task: handles, output progress and its first fault. Owned by execution, never
 * serialized and never replaced by a publication. The records file holds only `Published` rows. */
type Execution = {
  readonly id: string;
  /** The row proposed at creation; the published row supersedes it once its write lands. */
  readonly proposal: BashRecord;
  readonly logPaths: BashLogPaths;
  task: ShellTask | null;
  outputQueue: Promise<void>;
  readonly finished: Promise<void>;
  readonly resolveFinished: () => void;
  readonly rejectFinished: (error: Error) => void;
  terminationReason: BashStatus | null;
  foregroundFinish: ((result: BashExecuteResult) => void) | null;
  pingTimer: NodeJS.Timeout | null;
  /** Progress stays in memory; each lifecycle write stamps it into the file. */
  logBytes: number;
  /** First task-fatal fault. Claimed synchronously and shown at once; published when storage
   * permits. `endedAt` is set once the owned group is confirmed gone. */
  fault: { error: string; endedAt: number | null } | null;
  repair: Promise<void> | null;
};

/** A process owned by this runtime plus the independent observation that its creation row landed. */
type OwnedLaunch = {
  execution: Execution;
  creation: Promise<void>;
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

type DeliveredLog =
  | { log_state: "available"; output: string; truncated: boolean }
  | { log_state: "missing" | "unreadable"; output: ""; truncated: false; log_error: string };

export type BashForegroundResult = TaskOutcome & {
  kind: "foreground";
  exit_code: number;
  stdout: LogReadResult;
  stderr: LogReadResult;
};

export type BashOutputResult = BashOutcome & DeliveredLog & {
  kind: "output";
  bash_id: string;
};

export type BashKillResult = {
  kind: "kill";
  bash_id: string;
  killed: boolean;
  message: string;
};

type WaitFields = TaskOutcome & {
  kind: "wait";
  bash_id: string;
  duration_secs?: number;
  message?: string;
  checkpoint?: boolean;
  pending_ids?: string[];
};

export type BashWaitResult =
  | (WaitFields & { source: "bash"; delivery: "pending"; status: BashStatus; exit_code: number | null })
  | (WaitFields & { source: "bash"; delivery: "result" } & DeliveredLog)
  | (WaitFields & { source: "spawn"; delivery: "result" | "pending"; output: string });

export type BashDetachResult = {
  kind: "detach";
  bash_id: string;
  detached: boolean;
  message: string;
};

export type BashManageResult =
  | { kind: "list"; rows: BashListRow[] }
  | BashOutputResult
  | BashKillResult
  | BashWaitResult
  | BashDetachResult
  | { kind: "inspection"; bash_id: string; output: string; truncated: boolean; error?: string }
  | { kind: "error"; error: string };

const DEFAULT_TIMEOUT_MIN = 55;
// Wait as long as a foreground command may run; a short wait lets the model fetch
// results around consumption, so the idle watcher announces them again.
const WAIT_TIMEOUT_MIN = DEFAULT_TIMEOUT_MIN;
const MAX_TIMEOUT_MIN = 59;
const LOG_TAIL_BYTES = 4 * 1024;
const DEFAULT_TASK_PING_INTERVAL_MS = 55 * 60_000;
const TASK_PING_INTERVAL_ENV = "_MERIDIAN_PI_TASK_PING_INTERVAL_MS";
const TASK_PING_RESET_ON_ACTIVITY_ENV = "_MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY";
export const USER_BASH_PANEL_BACKGROUND_MSG = "Sent to background — /ps";

const OWNERSHIP_LOST = "ownership_lost: previous runtime exited; inspect manually or use bash_manage detach to release tracking";

const ownersKey = Symbol.for("meridian.pi.managed-bash.owners.v2");
const scope = globalThis as typeof globalThis & { [ownersKey]?: Map<string, BashRuntime> };
const owners = scope[ownersKey] ??= new Map<string, BashRuntime>();

export class BashRuntime {
  private readonly spawnId = currentRunIdFromEnv();
  private readonly recordsPath = resolveBashRecordsPath(this.spawnId);
  private readonly receiptsPath = resolveDeliveryReceiptsPath(this.spawnId);
  private readonly logStore = new BashLogStore(resolveBashLogsDir(this.spawnId));
  private readonly publication = new BashPublication({
    path: this.recordsPath,
    spawnId: this.spawnId,
    evidence: (published) => this.withEvidence(published),
  });
  private readonly executions = new Map<string, Execution>();
  private ready: Promise<void> = Promise.resolve();
  /** The latest fault text, claimed in memory before any write can carry it to the file. */
  private notice: string | undefined;

  constructor(private hooks: BashRuntimeHooks = {}) {
    const key = this.recordsPath;
    const prior = owners.get(key);
    if (prior) {
      prior.hooks = hooks;
      for (const exec of prior.executions.values()) prior.schedulePing(exec);
      return prior;
    }
    owners.set(key, this);
    this.ready = this.recoverRecords();
    // Construction cannot await recovery; commands observe its rejection.
    void this.ready.catch(() => undefined);
  }

  async execute(params: BashParams, signal: AbortSignal | undefined): Promise<BashExecuteResult> {
    const timeoutMin = normalizeTimeoutMin(params.timeout_min, DEFAULT_TIMEOUT_MIN);
    const launch = await this.launchRecord(params.command, timeoutMin, process.cwd(), process.env, undefined, signal);
    if (!launch) return abortedBeforeLaunch();
    const { execution: exec, creation } = launch;

    if (params.background === true) {
      await creation;
      if (!await this.publishBackground(exec, "launch")) throw new Error(`Background publication declined for ${exec.id}`);
      return { kind: "start", bash_id: exec.id, status: "started" };
    }

    this.hooks.onForegroundStart?.(exec.id);
    return await new Promise<BashExecuteResult>((resolve, reject) => {
      let settled = false;
      const close = (): boolean => {
        if (settled) return false;
        settled = true;
        exec.foregroundFinish = null;
        clearTimeout(timeout);
        signal?.removeEventListener("abort", abort);
        this.hooks.onForegroundStop?.(exec.id);
        return true;
      };
      const finish = (result: BashExecuteResult): void => {
        if (close()) resolve(result);
      };
      const fail = (error: unknown): void => { if (close()) reject(error); };
      exec.foregroundFinish = finish;
      const abort = (): void => {
        void this.stopForeground(exec).then(async () => finish(await this.readSplitLog(exec, {
          status: "aborted", exit_code: -1,
        }))).catch((error) => finish({ kind: "error", error: errorText(error) }));
      };
      const timeout = setTimeout(() => {
        void this.publishBackground(exec).then((published) => {
          if (published) finish({
            kind: "start",
            bash_id: exec.id,
            status: "backgrounded",
            message: `Command exceeded timeout_min=${timeoutMin} and was backgrounded as ${exec.id}. Use /ps to manage it.`,
          });
        }, (error) => finish({ kind: "error", error: errorText(error) }));
      }, timeoutMin * 60_000);

      if (signal?.aborted) {
        abort();
        return;
      }
      signal?.addEventListener("abort", abort, { once: true });
      void creation.catch(fail);
      void exec.finished.then(async () => {
        if (settled) return;
        await creation;
        if (settled) return;
        const output = await this.readSplitLog(exec);
        finish(output);
      }).catch(async (error) => {
        if (settled) return;
        try { await creation; }
        catch (creationError) { fail(creationError); return; }
        if (settled) return;
        const record = this.view(exec);
        finish(await this.readSplitLog(exec, { ...bashOutcome(record), error: record.execution_error ?? errorText(error) }));
      });
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
    const launch = await this.launchRecord(command, DEFAULT_TIMEOUT_MIN, cwd, { ...process.env, ...(options.env ?? {}) }, forwardOutput, options.signal);
    if (!launch) return { exitCode: -1 };
    const { execution: exec, creation } = launch;
    this.hooks.onForegroundStart?.(exec.id);

    return await new Promise<{ exitCode: number | null }>((resolve, reject) => {
      let settled = false;
      const close = (): boolean => {
        if (settled) return false;
        settled = true;
        forwardForegroundOutput = false;
        exec.foregroundFinish = null;
        options.signal?.removeEventListener("abort", abort);
        this.hooks.onForegroundStop?.(exec.id);
        return true;
      };
      const finish = (exitCode: number | null): void => {
        if (close()) resolve({ exitCode });
      };
      const fail = (error: unknown): void => { if (close()) reject(error); };
      const abort = (): void => {
        forwardForegroundOutput = false;
        void this.stopForeground(exec).then(() => finish(-1), () => finish(-1));
      };
      exec.foregroundFinish = () => {
        safeOnData(options.onData, Buffer.from(`${USER_BASH_PANEL_BACKGROUND_MSG}\n`, "utf-8"));
        finish(0);
      };

      if (options.signal?.aborted) {
        abort();
        return;
      }
      options.signal?.addEventListener("abort", abort, { once: true });
      void creation.catch(fail);
      void exec.finished.then(async () => {
        await creation;
        finish(this.view(exec).exit_code);
      }, async () => {
        try { await creation; }
        catch (creationError) { fail(creationError); return; }
        finish(-1);
      });
    });
  }

  async startDetachedUserBash(command: string, cwd: string, env: NodeJS.ProcessEnv = process.env): Promise<{ bash_id: string }> {
    const launch = await this.launchRecord(command, DEFAULT_TIMEOUT_MIN, cwd, env);
    if (!launch) throw new Error("Detached Bash launch was cancelled before start");
    const { execution: exec, creation } = launch;
    await creation;
    if (!await this.publishBackground(exec, "launch")) throw new Error(`Background publication declined for ${exec.id}`);
    return { bash_id: exec.id };
  }

  async backgroundForeground(): Promise<{ ok: boolean; reason?: string; bash_id?: string }> {
    const foreground = [...this.executions.values()]
      .filter((exec) => {
        const row = this.view(exec);
        return row.status === "running" && !row.is_background && exec.foregroundFinish != null;
      })
      .sort((a, b) => b.proposal.started_at_ms - a.proposal.started_at_ms)[0];
    if (!foreground) {
      return { ok: false, reason: "no_foreground" };
    }

    if (!await this.publishBackground(foreground)) {
      return { ok: false, reason: "foreground_not_published" };
    }
    foreground.foregroundFinish?.({
      kind: "start",
      bash_id: foreground.id,
      status: "backgrounded",
      message: USER_BASH_PANEL_BACKGROUND_MSG,
    });
    return { ok: true, bash_id: foreground.id };
  }

  list(includeCompleted: boolean): BashListRow[] {
    return [...this.executions.values()]
      .map((exec) => this.view(exec))
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
    let cleared: string[] = [];
    await this.publication.publish({
      stage: (base) => {
        cleared = [...base.rows.values()]
          .filter((row) => row.status !== "running" && (!row.is_background || !row.is_tracked || row.notification_consumed_at_ms != null || admitted.has(row.bash_id)))
          .map((row) => row.bash_id);
        return cleared.length === 0 ? null : withoutRows(base, cleared);
      },
      adopted: () => { for (const id of cleared) this.executions.delete(id); },
    });
    return cleared.length;
  }

  async manage(params: BashManageParams, signal?: AbortSignal): Promise<BashManageResult> {
    await this.prepare();
    const action = params.action;
    if (action === "list") {
      return { kind: "list", rows: this.list(params.include_completed === true) };
    }

    const id = params.bash_id?.trim();
    if (!id) {
      return { kind: "error", error: `bash_id is required for action '${action}'` };
    }

    const kind = classifyWorkId(id);
    if (kind === "spawn") {
      return await this.manageSpawn(id, params, signal);
    }
    if (kind !== "bash") {
      return { kind: "error", error: `unsupported id: ${id}` };
    }

    const exec = this.executions.get(id);
    if (!exec) {
      return { kind: "error", error: `bash_id not found: ${id}` };
    }

    switch (action) {
      case "output": {
        // A terminal status is published after queued output, so a read that starts
        // after it is final; one that races completion has not seen the result.
        const { outcome, log } = await this.readOutcomeLog(exec, LOG_TAIL_BYTES);
        return { kind: "output", bash_id: id, ...outcome, ...deliveredLog(log) };
      }
      case "kill":
        return await this.killBash(id, "killed");
      case "wait": {
        return await this.waitBash(exec, signal);
      }
      case "detach": {
        // Not task-fatal: a failed release is the caller's error and leaves the task running.
        const released = await this.publication.publish({
          stage: changeRow(id, () => ({ is_tracked: false })),
          adopted: () => this.clearPing(exec),
        });
        // Success is what was adopted: a declined slot found no published row to release (its creation never landed).
        if (!released) return { kind: "detach", bash_id: id, detached: false, message: `${id} was not detached: no published record is available.` };
        return { kind: "detach", bash_id: id, detached: true, message: `${id} detached from quiescence tracking.` };
      }
      default:
        return { kind: "error", error: `unsupported action: ${String(action)}` };
    }
  }

  async shutdown(): Promise<void> {
    await this.ensureReady();
    const results = await Promise.allSettled([...this.executions.values()].map(async (exec) => {
      this.clearPing(exec);
      if (exec.task && this.view(exec).status === "running") await this.killBash(exec.id, "killed");
    }));
    await this.publication.publish({ stage: (base) => base });
    const failure = results.find((result) => result.status === "rejected");
    if (failure?.status === "rejected") throw failure.reason;
  }

  private async launchRecord(
    command: string,
    timeoutMin: number,
    cwd = process.cwd(),
    env: NodeJS.ProcessEnv = process.env,
    onData?: (data: Buffer) => void,
    signal?: AbortSignal,
  ): Promise<OwnedLaunch | null> {
    await this.prepare();
    if (signal?.aborted) return null;
    const bashId = makeBashId();
    const logPaths = await this.logStore.create(bashId);
    // No await exists between this check and spawn, so an abort observed during preparation can
    // never turn into a subsequently launched process.
    if (signal?.aborted) {
      await Promise.all(Object.values(logPaths).map((logPath) => rm(logPath, { force: true })));
      return null;
    }

    const task = new ShellTask(command, cwd, { ...env, _MERIDIAN_PI_BASH_ID: bashId });
    const child = task.child;
    const exec = makeExecution({
      bash_id: bashId,
      command,
      cwd,
      pid: child.pid ?? null,
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
    }, task);
    this.executions.set(bashId, exec);
    this.attachOutput(exec, task, onData);
    child.once("error", (error) => this.queueOutput(exec, `\n[failed to start command: ${error.message}]\n`, "stderr"));
    // Exit observation waits for the owned group and every preceding log write.
    void task.waitForExit().then(async (code) => {
      await exec.outputQueue;
      if (this.view(exec).status === "running") await this.markTerminal(exec, exec.terminationReason ?? "exited", code);
    }).catch((error) => this.claim(exec, error));

    // Creation is published as proposed: live progress and faults are stamped in by the publisher.
    const creation = this.publishRecord(exec, (base) => withRow(base, this.project(exec.proposal, exec))).then((published) => {
      if (!published) throw new Error(`Creation publication declined for ${exec.id}`);
    });
    // Foreground abort may return before observing creation. Keep its later failure supervised;
    // positive callers still await this same promise explicitly.
    void creation.catch(() => undefined);
    return { execution: exec, creation };
  }

  private attachOutput(exec: Execution, task: ShellTask, onData?: (data: Buffer) => void): void {
    const child = task.child;
    for (const stream of ["stdout", "stderr"] as const) {
      const output = child[stream];
      const decoder = new StringDecoder("utf-8");
      output?.on("error", (error) => this.claim(exec, error));
      output?.on("data", (chunk: string | Buffer) => {
        output.pause();
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, "utf-8");
        try { onData?.(buffer); }
        catch (error) { this.claim(exec, error); }
        void this.queueOutput(exec, decoder.write(buffer), stream).then(() => output.resume());
      });
      output?.once("end", () => { const tail = decoder.end(); if (tail) void this.queueOutput(exec, tail, stream); });
    }
  }

  private queueOutput(exec: Execution, chunk: string, stream: "stdout" | "stderr"): Promise<void> {
    exec.outputQueue = exec.outputQueue.then(async () => {
      if (!exec.fault) await this.appendLog(exec, chunk, stream);
    }).catch((error) => this.claim(exec, error));
    return exec.outputQueue;
  }

  private async appendLog(exec: Execution, chunk: string, stream: "stdout" | "stderr"): Promise<void> {
    // Progress stays in memory: the records file keeps the size from the last
    // lifecycle write, and the logs themselves are the output authority.
    exec.logBytes = await this.logStore.append(exec.logPaths, stream, chunk);
    if (shouldResetPingOnActivity()) this.schedulePing(exec);
  }

  private async markTerminal(
    exec: Execution,
    status: BashStatus,
    exitCode: number | null,
  ): Promise<void> {
    this.clearPing(exec);
    const endedAt = Date.now();
    // The row keeps its running status and the task its handle until this write lands, so no
    // observer can see, wait on, or consume an unpublished result.
    await this.publishRecord(
      exec,
      // Linearization point: success is staged only while no failure has claimed the record. A
      // failure claimed during the write is ordered after it: its repair overwrites the file, and
      // between the rename and that repair a crash can still recover the success.
      changeRow(exec.id, (row) => exec.fault || row.status !== "running" ? null : { status, exit_code: exitCode, ended_at_ms: endedAt }),
      () => {
        if (exec.fault) return; // the claimed failure owns the outcome
        exec.task = null;
        exec.resolveFinished();
      },
    );
  }

  private async killBash(bashId: string, reason: "killed" | "aborted"): Promise<BashKillResult> {
    const exec = this.executions.get(bashId);
    if (!exec) {
      return { kind: "kill", bash_id: bashId, killed: false, message: `bash_id not found: ${bashId}` };
    }
    const before = this.view(exec).status;
    if (before !== "running") {
      return { kind: "kill", bash_id: bashId, killed: false, message: `${bashId} is already ${before}` };
    }
    if (!exec.task) {
      return { kind: "kill", bash_id: bashId, killed: false, message: `${bashId}: ownership lost after restart; no PID was signalled. Inspect the process manually or use bash_manage detach to release tracking.` };
    }
    await this.stopTask(exec, exec.task);
    await exec.finished;
    // Report the published outcome: the task may have exited on its own just before the signal.
    const { status } = this.view(exec);
    if (status !== "killed") return { kind: "kill", bash_id: bashId, killed: false, message: `${bashId} is already ${status}` };
    return { kind: "kill", bash_id: bashId, killed: true, message: `${bashId} killed` };
  }

  /** Stop the owned group. Resolves once it is physically gone, before any publication. */
  private async stopTask(exec: Execution, task: ShellTask): Promise<void> {
    exec.terminationReason = "killed";
    try { await task.terminate(); }
    catch (error) { this.claim(exec, error); throw error; }
  }

  /** Caller interruption of a foreground command: wait for the physical stop and its flushed
   * output, never for metadata publication. The record publishes on its own and claims nothing here. */
  private async stopForeground(exec: Execution): Promise<void> {
    const task = exec.task;
    if (this.view(exec).status !== "running" || !task) return;
    await this.stopTask(exec, task);
    await task.closed;
    await exec.outputQueue;
  }

  private async waitBash(exec: Execution, signal?: AbortSignal): Promise<BashWaitResult> {
    const before = this.view(exec);
    const beforeOutcome = bashOutcome(before);
    if (before.status === "running" && exec.task && beforeOutcome.error === undefined) {
      await new Promise<void>((resolve) => {
        const done = (): void => { clearTimeout(timer); signal?.removeEventListener("abort", done); resolve(); };
        const timer = setTimeout(done, WAIT_TIMEOUT_MIN * 60_000);
        if (signal?.aborted) done();
        else signal?.addEventListener("abort", done, { once: true });
        void exec.finished.then(done, done);
      });
    }
    const record = this.view(exec);
    const recordOutcome = bashOutcome(record);
    if (record.status === "running") {
      if (signal?.aborted && recordOutcome.error === undefined) return interruptedBashWait(exec.id, recordOutcome);
      if (recordOutcome.error !== undefined) {
        const { outcome, log } = await this.readOutcomeLog(exec, 2 * 1024, signal);
        return { kind: "wait", source: "bash", delivery: "result", bash_id: exec.id,
          duration_secs: durationSecs(record), ...outcome, ...deliveredLog(log) };
      }
      return {
        kind: "wait",
        source: "bash",
        delivery: "pending",
        bash_id: exec.id,
        status: "running",
        exit_code: null,
        message: `Still running after ${WAIT_TIMEOUT_MIN} minutes. Call bash_manage(action='wait') again, or bash_manage(action='kill') to terminate. Reading its output any other way leaves the result unconsumed, and a completion notice will follow.`,
      };
    }
    const { outcome, log, acknowledgement } = await this.readOutcomeLog(exec, 2 * 1024, signal);
    if (signal?.aborted && acknowledgement === "declined" && outcome.error === undefined) {
      return interruptedBashWait(exec.id, outcome);
    }
    return {
      kind: "wait",
      source: "bash",
      delivery: "result",
      bash_id: exec.id,
      duration_secs: durationSecs(record),
      ...outcome,
      ...deliveredLog(log),
    };
  }

  private schedulePing(exec: Execution): void {
    this.clearPing(exec);
    if (!exec.task || !isPingable(this.view(exec))) return;
    const timer = setTimeout(() => { void this.firePing(exec).catch((error) => this.claim(exec, error)); }, taskPingIntervalMs());
    timer.unref();
    exec.pingTimer = timer;
  }

  private clearPing(exec: Execution): void {
    if (exec.pingTimer) clearTimeout(exec.pingTimer);
    exec.pingTimer = null;
  }

  private async firePing(exec: Execution): Promise<void> {
    exec.pingTimer = null;
    const notify = this.hooks.onBackgroundPing;
    if (!notify) return;
    // Both writes stage against published state: a notification obligation exists only for a live
    // tracked background task, so neither can race a detach or terminal write.
    const obligated = (row: BashRecord): boolean => !exec.fault && hasPingLifetime(row);
    const claimed = await this.publishRecord(exec, changeRow(exec.id, (row) =>
      obligated(row) && row.ping_sent_at_ms == null ? { ping_sent_at_ms: Date.now() } : null));
    if (!claimed) return;
    try { await notify(this.view(exec)); }
    catch (error) {
      // Notification capability can become stale during reload. It owns no
      // shell lifecycle: release its claim without turning it into task failure.
      process.stderr.write(`[managed-bash] ping ${exec.id}: ${errorText(error)}\n`);
      // A release queued behind the end of that lifetime has nothing to release and is declined.
      await this.publishRecord(exec, changeRow(exec.id, (row) => obligated(row) ? { ping_sent_at_ms: null } : null));
      // Do not spin on a broken capability. A new hook (including one rebound
      // while this attempt was in flight) can retry the released claim.
      if (this.hooks.onBackgroundPing !== notify) this.schedulePing(exec);
    }
  }

  private async manageSpawn(spawnId: string, params: BashManageParams, signal?: AbortSignal): Promise<BashManageResult> {
    switch (params.action) {
      case "output": {
        const result = await runMeridianCommand(["session", "log", spawnId, "--tail", "20"], 15_000);
        if (result.error) return { kind: "inspection", bash_id: spawnId, output: result.stderr || result.error, truncated: false, error: result.error };
        return { kind: "inspection", bash_id: spawnId, output: result.stdout || result.stderr, truncated: false };
      }
      case "kill": {
        const result = await runMeridianCommand(["spawn", "cancel", spawnId], 15_000);
        if (result.error) return { kind: "kill", bash_id: spawnId, killed: false, message: result.stderr || result.error };
        return { kind: "kill", bash_id: spawnId, killed: result.exitCode === 0, message: result.stdout || result.stderr };
      }
      case "wait":
        return waitForSpawn(spawnId, WAIT_TIMEOUT_MIN, signal);
      case "detach":
        return { kind: "detach", bash_id: spawnId, detached: false, message: "detach only applies to b-* bash records" };
      default:
        return { kind: "error", error: `unsupported p-* action: ${params.action}` };
    }
  }

  private async readSplitLog(exec: Execution, supplied?: TaskOutcome): Promise<BashForegroundResult> {
    const observed = supplied ?? bashOutcome(this.view(exec));
    const [stdout, stderr] = await Promise.all([
      this.readLog(exec, LOG_TAIL_BYTES, "stdout"),
      this.readLog(exec, LOG_TAIL_BYTES, "stderr"),
    ]);
    const current = this.view(exec);
    const outcome = current.execution_error !== undefined ? bashOutcome(current) : observed;
    return {
      kind: "foreground",
      ...outcome,
      exit_code: outcome.exit_code ?? -1,
      stdout,
      stderr,
    };
  }

  private async readLog(exec: Execution, maxBytes: number, stream: "combined" | "stdout" | "stderr" = "combined"): Promise<LogReadResult> {
    return this.logStore.readResult(exec.logPaths, stream, maxBytes);
  }

  /** Capture the outcome before I/O. Ordinary completion that races the read is not delivered;
   * negative fault evidence that appears during either the read or consumption is revalidated. */
  private async readOutcomeLog(exec: Execution, maxBytes: number, signal?: AbortSignal): Promise<{
    outcome: BashOutcome;
    log: LogReadResult;
    acknowledgement: "not_applicable" | "declined" | "adopted";
  }> {
    const observed = bashOutcome(this.view(exec));
    const log = await this.readLog(exec, maxBytes);
    let outcome = this.outcomeWithNewFault(exec, observed);
    let acknowledgement: "not_applicable" | "declined" | "adopted" = "not_applicable";
    if (log.kind === "available" && isTerminalBashStatus(observed.status) && outcome.error === undefined) {
      acknowledgement = await this.consumeDeliveredResult(exec, observed, signal) ? "adopted" : "declined";
      outcome = this.outcomeWithNewFault(exec, observed);
    }
    return { outcome, log, acknowledgement };
  }

  private outcomeWithNewFault(exec: Execution, observed: BashOutcome): BashOutcome {
    const current = this.view(exec);
    return current.execution_error !== undefined ? bashOutcome(current) : observed;
  }

  /** The row as readers see it: published (or proposed) metadata with live progress and the
   * negative fault overlay applied. */
  private view(exec: Execution): BashRecord {
    return this.project(this.publication.current.rows.get(exec.id) ?? exec.proposal, exec);
  }

  private project(row: Readonly<BashRecord>, exec: Execution | undefined = this.executions.get(row.bash_id)): BashRecord {
    if (!exec) return row;
    const { fault } = exec;
    const projected: BashRecord = {
      ...row,
      log_bytes: exec.logBytes,
      ...(fault ? { execution_error: fault.error } : {}),
      ...(fault?.endedAt != null ? { status: "killed" as const, exit_code: -1, ended_at_ms: fault.endedAt } : {}),
    };
    // Infrastructure failure is a new owed result even if an ordinary terminal outcome was
    // acknowledged immediately before the fault. Every repair publication carries that policy.
    if (fault) delete projected.notification_consumed_at_ms;
    return projected;
  }

  /** What every publication stages against: the published value with live evidence merged in.
   * Faults and progress are never retracted by a write, so any slot may carry them to the file. */
  private withEvidence(published: Published): Published {
    const rows = new Map<string, Readonly<BashRecord>>();
    for (const [id, row] of published.rows) rows.set(id, this.project(row));
    return withRuntimeError({ rows }, this.notice ?? published.runtimeError);
  }

  /** A task-fatal publication. A failed stage or write claims `exec` inside its own queue slot (see
   * `BashPublication`); callers see the original error once its cleanup has run. */
  private async publishRecord(
    exec: Execution,
    stage: (base: Published) => Published | null,
    adopted?: () => void,
  ): Promise<boolean> {
    try { return await this.publication.publish({ stage, adopted, fatal: (error) => this.claim(exec, error) }); }
    catch (error) { await exec.repair; throw error; }
  }

  private async publishBackground(exec: Execution, phase: "launch" | "handoff" = "handoff"): Promise<boolean> {
    const published = await this.publishRecord(exec, changeRow(exec.id, (row) =>
      !exec.fault && (phase === "launch" || row.status === "running") && !row.is_background ? { is_background: true } : null));
    if (published) this.schedulePing(exec);
    return published;
  }

  private async prepare(): Promise<void> {
    await this.ensureReady();
    const stale = this.notice ?? this.publication.current.runtimeError;
    if (stale === undefined) return;
    await this.publication.publish({
      stage: (base) => base.runtimeError !== stale ? null : withRuntimeError(base, undefined),
      adopted: () => { if (this.notice === stale) this.notice = undefined; },
    });
  }

  private async ensureReady(): Promise<void> {
    const observed = this.ready;
    try { await observed; }
    catch {
      // An operator can repair unreadable cold-start evidence without needing to abandon the
      // process. Never replace it with an empty map. Recovery publishes the whole recovered value,
      // so exactly one may be in flight: only the first caller to see this failure restarts it and
      // later callers join that attempt; a stale second recovery could overwrite a newer publication.
      if (this.ready === observed) this.ready = this.recoverRecords();
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
    const rows = new Map<string, BashRecord>();
    for (const row of Object.values(file.records)) {
      // Durable fault evidence is never replaced; ownership loss is added once.
      const lost = row.status === "running" && !row.execution_error?.includes(OWNERSHIP_LOST);
      rows.set(row.bash_id, lost ? { ...row, execution_error: row.execution_error ? `${row.execution_error}; ${OWNERSHIP_LOST}` : OWNERSHIP_LOST } : row);
    }
    // A recovered running row is unresolved work, not permission to signal a possibly reused PID.
    // Publish its diagnostic before accepting commands; only then does it become live.
    await this.publication.publish({
      stage: () => withRuntimeError({ rows }, file.runtime_error),
      adopted: () => { for (const row of rows.values()) this.executions.set(row.bash_id, makeExecution(row, null)); },
    });
  }

  /** Claim a task-fatal fault, synchronously: the first fault owns the outcome and is visible at
   * once. It returns no completion promise: output drain and physical stop must not inherit
   * repair's metadata dependency. Publication observers use `finished` or `repair` explicitly. */
  private claim(exec: Execution, error: unknown): void {
    if (exec.fault || this.view(exec).execution_error) return;
    const fault = { error: errorText(error), endedAt: null as number | null };
    exec.fault = fault;
    exec.terminationReason = "killed";
    this.notice = fault.error;
    this.clearPing(exec);
    // No further output can be published. Closing owned pipes avoids tying
    // cleanup to paused read buffers or a failed output queue.
    exec.task?.child.stdout?.destroy();
    exec.task?.child.stderr?.destroy();
    exec.repair = Promise.resolve().then(() => this.repair(exec, fault));
  }

  private async repair(exec: Execution, fault: { error: string; endedAt: number | null }): Promise<void> {
    try {
      if (exec.task) await exec.task.terminate();
      exec.task = null;
      fault.endedAt = Date.now();
    } catch (cleanupError) {
      fault.error += `; cleanup failed: ${errorText(cleanupError)}`;
    }
    try {
      // Failure evidence publishes as it stands, even when the creation write never landed.
      await this.publication.publish({ stage: (base) => withRow(base, this.project(base.rows.get(exec.id) ?? exec.proposal, exec)) });
    } catch (publishError) {
      process.stderr.write(`[managed-bash] task ${exec.id}: ${errorText(publishError)}\n`);
    }
    exec.rejectFinished(new Error(fault.error));
  }

  /** When the model received a terminal result, its completion notice would repeat it.
   * Execution failures stay owed: their notice carries the error the log lacks. */
  private async consumeDeliveredResult(exec: Execution, delivered: BashOutcome, signal?: AbortSignal): Promise<boolean> {
    // Not task-fatal. Staged in the slot: a write queued ahead of this one can fail the record meanwhile.
    return await this.publication.publish({
      stage: changeRow(exec.id, (row) => {
        const current = bashOutcome(row);
        return signal?.aborted || current.error !== undefined || this.executions.get(exec.id) !== exec
          || current.status !== delivered.status || current.exit_code !== delivered.exit_code
          ? null : { notification_consumed_at_ms: Date.now() };
      }),
    });
  }
}

function interruptedBashWait(id: string, outcome: BashOutcome): BashWaitResult {
  const state = outcome.status === "running" ? "while it was still running" : `before its ${outcome.status} result was acknowledged`;
  return { kind: "wait", source: "bash", delivery: "pending", bash_id: id, ...outcome,
    message: `Wait for ${id} interrupted ${state}. The result remains owed; wait again to collect it.` };
}

function abortedBeforeLaunch(): BashForegroundResult {
  return {
    kind: "foreground",
    status: "aborted",
    exit_code: -1,
    stdout: { kind: "available", text: "", truncated: false },
    stderr: { kind: "available", text: "", truncated: false },
  };
}

function deliveredLog(result: LogReadResult): DeliveredLog {
  return result.kind === "available"
    ? { log_state: "available", output: result.text, truncated: result.truncated }
    : { log_state: result.kind, output: "", truncated: false, log_error: result.error };
}

function taskPingIntervalMs(): number {
  const raw = Number.parseInt(process.env[TASK_PING_INTERVAL_ENV] ?? "", 10);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_TASK_PING_INTERVAL_MS;
}

function shouldResetPingOnActivity(): boolean {
  return (process.env[TASK_PING_RESET_ON_ACTIVITY_ENV] ?? "true").toLowerCase() !== "false";
}

/** A tracked, background, still-running task: the only kind that owes a notification ping. */
function hasPingLifetime(record: BashRecord): boolean {
  return record.status === "running" && record.is_background && record.is_tracked;
}

/** Within that lifetime, a task whose one-shot ping has not been claimed. */
function isPingable(record: BashRecord): boolean {
  return hasPingLifetime(record) && record.ping_sent_at_ms == null;
}

function makeExecution(proposal: BashRecord, task: ShellTask | null): Execution {
  let resolveFinished!: () => void;
  let rejectFinished!: (error: Error) => void;
  const finished = new Promise<void>((resolve, reject) => { resolveFinished = resolve; rejectFinished = reject; });
  void finished.catch(() => undefined);
  if (proposal.status !== "running") resolveFinished();
  return { id: proposal.bash_id, proposal, logPaths: logPathsFromRecord(proposal), task, outputQueue: Promise.resolve(),
    finished, resolveFinished, rejectFinished, terminationReason: null, foregroundFinish: null, pingTimer: null,
    logBytes: proposal.log_bytes, fault: null, repair: null };
}

function errorText(error: unknown): string {
  // Failure claiming must not itself fail on an arbitrary thrown callback value.
  try { return (error instanceof Error ? error.message : String(error)) || "unspecified failure"; }
  catch { return "unprintable failure"; }
}

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
