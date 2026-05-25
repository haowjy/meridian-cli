import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { classifyWorkId } from "../../shared/ids";
import { writeJsonAtomic } from "../../shared/json_file";
import { runMeridianCommand } from "../../shared/meridian_cli";
import {
  currentSpawnIdFromEnv,
  resolveBashLogsDir,
  resolveBashRecordsPath,
} from "../../shared/pi_state_paths";
import type { BashRecord, BashRecordsFile, BashStatus } from "../../shared/schemas";

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
  child: ChildProcess | null;
  waiters: Array<() => void>;
  foregroundFinish: ((result: unknown) => void) | null;
};

export type BashRuntimeHooks = {
  onForegroundStart?: (bashId: string) => void;
  onForegroundStop?: (bashId: string) => void;
};

type ExecResult = {
  stdout: string;
  stderr: string;
  exit_code: number;
};

const DEFAULT_TIMEOUT_MIN = 55;
const DEFAULT_WAIT_TIMEOUT_MIN = 10;
const MAX_TIMEOUT_MIN = 59;
const LOG_TAIL_BYTES = 4 * 1024;
export const USER_BASH_PANEL_BACKGROUND_MSG = "Sent to background — /ps";

export class BashRuntime {
  private readonly spawnId = currentSpawnIdFromEnv();
  private readonly records = new Map<string, RuntimeRecord>();

  constructor(private readonly hooks: BashRuntimeHooks = {}) {}

  async execute(params: BashParams, signal: AbortSignal | undefined): Promise<unknown> {
    const timeoutMin = normalizeTimeoutMin(params.timeout_min, DEFAULT_TIMEOUT_MIN);
    const record = await this.startRecord(params.command, timeoutMin);

    if (params.background === true) {
      record.is_background = true;
      await this.persist();
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
      const abort = async (): Promise<void> => {
        await this.killBash(record.bash_id, "aborted");
        finish({ stdout: await this.readLog(record, LOG_TAIL_BYTES), stderr: "[command aborted]", exit_code: -1 });
      };
      const timeout = setTimeout(async () => {
        record.is_background = true;
        await this.persist();
        finish({
          bash_id: record.bash_id,
          status: "backgrounded",
          message: `Command exceeded timeout_min=${timeoutMin} and was backgrounded as ${record.bash_id}. Use /ps to manage it.`,
        });
      }, timeoutMin * 60_000);

      if (signal?.aborted) {
        void abort();
        return;
      }
      signal?.addEventListener("abort", () => void abort(), { once: true });
      this.onTerminal(record, async () => {
        if (settled) return;
        const output = await this.readSplitLog(record);
        finish(output);
      });
    });
  }

  async executeUserBash(
    command: string,
    cwd: string,
    options: UserBashExecOptions = {},
  ): Promise<{ exitCode: number | null }> {
    const record = await this.startRecord(command, DEFAULT_TIMEOUT_MIN, cwd, { ...process.env, ...(options.env ?? {}) }, options.onData);
    this.hooks.onForegroundStart?.(record.bash_id);

    return await new Promise<{ exitCode: number | null }>((resolve) => {
      let settled = false;
      const finish = (exitCode: number | null): void => {
        if (settled) return;
        settled = true;
        record.foregroundFinish = null;
        options.signal?.removeEventListener("abort", abort);
        this.hooks.onForegroundStop?.(record.bash_id);
        resolve({ exitCode });
      };
      const abort = (): void => {
        void this.killBash(record.bash_id, "aborted").finally(() => finish(-1));
      };
      record.foregroundFinish = () => {
        options.onData?.(Buffer.from(`${USER_BASH_PANEL_BACKGROUND_MSG}\n`, "utf-8"));
        finish(0);
      };

      if (options.signal?.aborted) {
        abort();
        return;
      }
      options.signal?.addEventListener("abort", abort, { once: true });
      this.onTerminal(record, () => finish(record.exit_code));
    });
  }

  async startDetachedUserBash(command: string, cwd: string, env: NodeJS.ProcessEnv = process.env): Promise<{ bash_id: string }> {
    const record = await this.startRecord(command, DEFAULT_TIMEOUT_MIN, cwd, env);
    record.is_background = true;
    await this.persist();
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
    await this.persist();
    foreground.foregroundFinish?.({
      bash_id: foreground.bash_id,
      status: "backgrounded",
      message: USER_BASH_PANEL_BACKGROUND_MSG,
    });
    return { ok: true, bash_id: foreground.bash_id };
  }

  async manage(params: BashManageParams): Promise<unknown> {
    const action = params.action;
    if (action === "list") {
      return { rows: this.listRows(params.include_completed === true) };
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
      case "wait":
        return await this.waitBash(record, normalizeTimeoutMin(params.timeout_min, DEFAULT_WAIT_TIMEOUT_MIN));
      case "detach":
        record.is_tracked = false;
        await this.persist();
        return { bash_id: id, detached: true, message: `${id} detached from quiescence tracking.` };
      default:
        return { error: `unsupported action: ${String(action)}` };
    }
  }

  async shutdown(): Promise<void> {
    for (const record of this.records.values()) {
      if (record.child && record.status === "running") {
        try {
          record.child.kill("SIGTERM");
        } catch {
          // ignore process-race failures
        }
      }
    }
    await this.persist();
  }

  private async startRecord(
    command: string,
    timeoutMin: number,
    cwd = process.cwd(),
    env: NodeJS.ProcessEnv = process.env,
    onData?: (data: Buffer) => void,
  ): Promise<RuntimeRecord> {
    const bashId = makeBashId();
    const logsDir = resolveBashLogsDir(this.spawnId);
    await mkdir(logsDir, { recursive: true });
    const logPath = path.join(logsDir, `${bashId}.log`);
    await writeFile(logPath, "", "utf-8");

    const record: RuntimeRecord = {
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
      log_path: logPath,
      log_bytes: 0,
      timeout_min: timeoutMin,
      originating_bash_id: process.env.MERIDIAN_PI_BASH_ID || null,
      child: null,
      waiters: [],
      foregroundFinish: null,
    };
    this.records.set(bashId, record);

    const child = spawn(command, {
      cwd,
      env: { ...env, MERIDIAN_PI_BASH_ID: bashId },
      shell: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    record.child = child;
    record.pid = child.pid ?? null;
    this.attachOutput(record, child, onData);

    child.once("error", async (error) => {
      await this.appendLog(record, `\n[failed to start command: ${error.message}]\n`);
      await this.markTerminal(record, "killed", -1);
    });
    child.once("close", async (code) => {
      if (record.status !== "running") return;
      await this.markTerminal(record, "exited", code ?? -1);
    });

    await this.persist();
    return record;
  }

  private attachOutput(record: RuntimeRecord, child: ChildProcess, onData?: (data: Buffer) => void): void {
    child.stdout?.on("data", (chunk: string | Buffer) => {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, "utf-8");
      onData?.(buffer);
      void this.appendLog(record, buffer.toString("utf-8"));
    });
    child.stderr?.on("data", (chunk: string | Buffer) => {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, "utf-8");
      onData?.(buffer);
      void this.appendLog(record, buffer.toString("utf-8"));
    });
  }

  private async appendLog(record: RuntimeRecord, chunk: string): Promise<void> {
    await writeFile(record.log_path, chunk, { encoding: "utf-8", flag: "a" });
    try {
      record.log_bytes = (await stat(record.log_path)).size;
    } catch {
      record.log_bytes += Buffer.byteLength(chunk, "utf-8");
    }
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
    record.child = null;
    const waiters = record.waiters.splice(0);
    for (const waiter of waiters) waiter();
    await this.persist();
  }

  private async killBash(bashId: string, reason: "killed" | "aborted"): Promise<unknown> {
    const record = this.records.get(bashId);
    if (!record) {
      return { bash_id: bashId, killed: false, message: `bash_id not found: ${bashId}` };
    }
    if (record.status !== "running") {
      return { bash_id: bashId, killed: false, message: `${bashId} is already ${record.status}` };
    }
    try {
      record.child?.kill("SIGTERM");
    } catch {
      // ignore process-race failures
    }
    await this.markTerminal(record, reason === "aborted" ? "killed" : "killed", -1);
    return { bash_id: bashId, killed: true, message: `${bashId} killed` };
  }

  private async waitBash(record: RuntimeRecord, timeoutMin: number): Promise<unknown> {
    if (record.status === "running") {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, timeoutMin * 60_000);
        this.onTerminal(record, () => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
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
      output: await this.readLog(record, 2 * 1024),
    };
  }

  private onTerminal(record: RuntimeRecord, fn: () => void | Promise<void>): void {
    if (record.status !== "running") {
      void fn();
      return;
    }
    record.waiters.push(() => void fn());
  }

  private listRows(includeCompleted: boolean): Array<Record<string, unknown>> {
    return [...this.records.values()]
      .filter((record) => includeCompleted || record.status === "running")
      .map((record) => ({
        type: "bash",
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
        log_bytes: record.log_bytes,
        timeout_min: record.timeout_min,
        originating_bash_id: record.originating_bash_id,
      }));
  }

  private async manageSpawn(spawnId: string, params: BashManageParams): Promise<unknown> {
    switch (params.action) {
      case "output": {
        const result = await runMeridianCommand(["session", "log", spawnId, "-n", "20"], 15_000);
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
      stdout: await this.readLog(record, LOG_TAIL_BYTES),
      stderr: "",
      exit_code: record.exit_code ?? -1,
    };
  }

  private async readLog(record: RuntimeRecord, maxBytes: number): Promise<string> {
    try {
      const content = await readFile(record.log_path, "utf-8");
      return content.slice(Math.max(0, content.length - maxBytes));
    } catch {
      return "";
    }
  }

  private async persist(): Promise<void> {
    const records: Record<string, BashRecord> = {};
    for (const [id, record] of this.records.entries()) {
      const { child: _child, waiters: _waiters, foregroundFinish: _foregroundFinish, ...plain } = record;
      records[id] = plain;
    }
    const file: BashRecordsFile = {
      v: 1,
      spawn_id: this.spawnId,
      updated_at_ms: Date.now(),
      records,
    };
    await writeJsonAtomic(resolveBashRecordsPath(this.spawnId), file);
  }
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

function durationSecs(record: BashRecord): number {
  return Math.max(0, ((record.ended_at_ms ?? Date.now()) - record.started_at_ms) / 1000);
}
