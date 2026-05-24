import type { MeridianEventBus } from "../../shared/meridian_event_bus";
import {
  PING_SCAN_INTERVAL_MS,
  TASK_OUTPUT_EVENT,
  TASK_OUTPUT_THROTTLE_MS,
  type StoredTaskRecord,
} from "./task_constants";
import type { TaskStore } from "./task_store";
import { nowMs } from "./task_utils";
import type { RuntimeTask } from "./task_process_types";
import type { SpawnTaskPingDefaults } from "./session_ping";
import { resolveEffectivePingIntervalMs } from "./session_ping";
import type { TaskEvents } from "./task_events";

export class TaskPing {
  private pingTimer: NodeJS.Timeout | null = null;
  private readonly lastOutputEmitAt = new Map<string, number>();
  private readonly pendingOutputEmit = new Map<string, NodeJS.Timeout>();

  constructor(
    private readonly jobs: Map<string, RuntimeTask>,
    private readonly store: TaskStore,
    private readonly bus: MeridianEventBus,
    private readonly events: TaskEvents,
    private readonly spawnPingDefaults: SpawnTaskPingDefaults,
  ) {}

  effectivePingIntervalMs(record: StoredTaskRecord): number {
    return resolveEffectivePingIntervalMs(
      record.ping_interval_ms,
      this.spawnPingDefaults.pingIntervalMs,
    );
  }

  initializePingSchedule(record: StoredTaskRecord, atMs: number = nowMs()): void {
    record.last_activity_at_ms = atMs;
    record.next_ping_at_ms = atMs + this.effectivePingIntervalMs(record);
  }

  startPingWorker(): void {
    if (this.pingTimer != null) {
      return;
    }
    this.pingTimer = setInterval(() => {
      void this.scanTaskPings();
    }, PING_SCAN_INTERVAL_MS);
    this.pingTimer.unref?.();
  }

  stopPingWorker(): void {
    if (this.pingTimer == null) {
      return;
    }
    clearInterval(this.pingTimer);
    this.pingTimer = null;
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
    void this.store.persistRecord(record);
  }

  clearTaskOutputNotify(taskId: string): void {
    const timeout = this.pendingOutputEmit.get(taskId);
    if (timeout) {
      clearTimeout(timeout);
    }
    this.pendingOutputEmit.delete(taskId);
    this.lastOutputEmitAt.delete(taskId);
  }

  clearAllOutputNotify(): void {
    for (const taskId of this.pendingOutputEmit.keys()) {
      this.clearTaskOutputNotify(taskId);
    }
  }

  notifyTaskOutput(taskId: string): void {
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
      this.events.emitTaskPing(record);
      record.next_ping_at_ms = now + this.effectivePingIntervalMs(record);
      await this.store.persistRecord(record);
    }
  }
}
