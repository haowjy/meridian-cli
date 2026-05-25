import { createLocalBus, type MeridianEventBus } from "../../shared/meridian_event_bus";
import { isMeridianSpawnCommand } from "../../shared/meridian_spawn";
import type { LifecycleSidecarWriter } from "../../shared/lifecycle_sidecar";
import { resolveStateRoot } from "../../shared/pi_state_paths";
import type { BackgroundTaskRecord, TaskIngress, WaitPolicy } from "./types";
import {
  DEFAULT_BG_READ_BYTES,
  DEFAULT_BG_WAIT_TIMEOUT_MS,
  MAX_BG_READ_BYTES,
  MAX_BG_WAIT_TIMEOUT_MS,
} from "./task_constants";
import { TaskEvents } from "./task_events";
import { TaskPing } from "./task_ping";
import { TaskProcess } from "./task_process";
import type { RuntimeTask } from "./task_process_types";
import { TaskStore } from "./task_store";
import { makeId, normalizeWaitPolicy, trimCombinedTails } from "./task_utils";
import {
  resolveSpawnTaskPingDefaults,
  type SpawnTaskPingDefaults,
} from "./session_ping";

export { resolveStateRoot };
export {
  DEFAULT_BG_READ_BYTES,
  DEFAULT_BG_WAIT_TIMEOUT_MS,
  MAX_BG_READ_BYTES,
  MAX_BG_WAIT_TIMEOUT_MS,
};
export {
  clamp,
  toInt,
  makeId,
  normalizeWaitPolicy,
  isMeridianSpawnCommand,
  trimCombinedTails,
} from "./task_utils";

export class TaskRegistry {
  private readonly jobs: Map<string, RuntimeTask> = new Map();
  private readonly store: TaskStore;
  private readonly events: TaskEvents;
  private readonly ping: TaskPing;
  private readonly process: TaskProcess;

  constructor(
    stateRoot: string,
    sessionId: string,
    parentSpawnId: string | null,
    bus: MeridianEventBus = createLocalBus(),
    sidecar: LifecycleSidecarWriter | null = null,
    spawnPingDefaults: SpawnTaskPingDefaults = resolveSpawnTaskPingDefaults(),
  ) {
    this.store = new TaskStore(stateRoot, sessionId);
    this.events = new TaskEvents(
      sessionId,
      parentSpawnId,
      bus,
      sidecar,
      spawnPingDefaults,
      (record) => this.ping.effectivePingIntervalMs(record),
    );
    this.ping = new TaskPing(this.jobs, this.store, bus, this.events, spawnPingDefaults);
    this.process = new TaskProcess(
      this.jobs,
      this.store,
      this.events,
      this.ping,
      spawnPingDefaults,
    );
  }

  async initialize(): Promise<void> {
    await this.store.ensureTasksDir();
    await this.process.orphanScan();
    this.process.startPoller();
    this.ping.startPingWorker();
  }

  bumpTaskActivity(taskId: string): void {
    this.ping.bumpTaskActivity(taskId);
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
  ) {
    return this.process.startJob(command, waitPolicy, cwd, env, label, options);
  }

  async detachJob(jobId: string): Promise<BackgroundTaskRecord | null> {
    return this.process.detachJob(jobId);
  }

  releaseWait(jobId: string): boolean {
    return this.process.releaseWait(jobId);
  }

  async waitForCompletion(jobId: string, timeoutMs: number): Promise<BackgroundTaskRecord | null> {
    return this.process.waitForCompletion(jobId, timeoutMs);
  }

  async getTask(taskId: string): Promise<BackgroundTaskRecord | null> {
    const runtimeJob = this.jobs.get(taskId);
    if (!runtimeJob) {
      return null;
    }
    return this.store.toPublicRecord(runtimeJob.record);
  }

  async list(includeCompleted: boolean): Promise<BackgroundTaskRecord[]> {
    return Array.from(this.jobs.values())
      .map((job) => this.store.toPublicRecord(job.record))
      .filter((record) => includeCompleted || record.status === "running")
      .sort((a, b) => b.started_at_ms - a.started_at_ms);
  }

  async syncBashToolRunning(input: Parameters<TaskProcess["syncBashToolRunning"]>[0]) {
    return this.process.syncBashToolRunning(input);
  }

  async syncBashToolExited(input: Parameters<TaskProcess["syncBashToolExited"]>[0]) {
    return this.process.syncBashToolExited(input);
  }

  async readLog(jobId: string, maxBytes: number, offset?: number) {
    return this.process.readLog(jobId, maxBytes, offset);
  }

  async killJob(jobId: string): Promise<BackgroundTaskRecord | null> {
    return this.process.killJob(jobId);
  }

  async shutdownCleanup(): Promise<void> {
    this.ping.stopPingWorker();
    this.ping.clearAllOutputNotify();
    await this.process.shutdownCleanup();
  }

  async clearFinished(): Promise<number> {
    for (const [taskId, runtimeJob] of [...this.jobs.entries()]) {
      if (runtimeJob.record.status === "running") {
        continue;
      }
      this.ping.clearTaskOutputNotify(taskId);
    }
    return this.store.clearFinished(this.jobs);
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

export function currentSpawnIdFromEnv(): string | null {
  const raw = process.env.MERIDIAN_SPAWN_ID?.trim() || "";
  return raw.length > 0 ? raw : null;
}

export function parentSpawnIdFromEnv(): string | null {
  const raw =
    process.env.MERIDIAN_PARENT_SPAWN_ID?.trim() ||
    process.env.MERIDIAN_SPAWN_ID?.trim() ||
    "";
  return raw.length > 0 ? raw : null;
}
