import type { LifecycleSidecarWriter } from "../../shared/lifecycle_sidecar";
import type { MeridianEventBus } from "../../shared/meridian_event_bus";
import {
  SUBSPAWN_END_EVENT,
  SUBSPAWN_START_EVENT,
  TASK_END_EVENT,
  TASK_PING_EVENT,
  TASK_START_EVENT,
  type StoredTaskRecord,
} from "./task_constants";
import { nowMs, truncateCommand } from "./task_utils";
import type { SpawnTaskPingDefaults } from "./session_ping";
import { resolveEffectivePingIntervalMs } from "./session_ping";

export class TaskEvents {
  constructor(
    private readonly sessionId: string,
    private readonly parentSpawnId: string | null,
    private readonly bus: MeridianEventBus,
    private readonly sidecar: LifecycleSidecarWriter | null,
    private readonly spawnPingDefaults: SpawnTaskPingDefaults,
    private readonly effectivePingIntervalMs: (record: StoredTaskRecord) => number,
  ) {}

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

  subspawnKind(record: StoredTaskRecord): string {
    return record.ingress === "bash" ? "bash" : "process";
  }

  sidecarSubspawnKind(record: StoredTaskRecord): string {
    return record.ingress === "bash" ? "bash" : this.subspawnKind(record);
  }

  private emitSidecar(event: Record<string, unknown>): void {
    this.sidecar?.append(event);
  }

  emitTaskStart(record: StoredTaskRecord): void {
    const command = truncateCommand(record.command);
    const kind = this.subspawnKind(record);
    const envelope = this.buildSubspawnEnvelope(kind, record.task_id);
    const payload = {
      type: "meridian.task.start",
      ...envelope,
      task_id: record.task_id,
      subspawn_id: record.task_id,
      wait_policy: record.wait_policy,
      command,
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
      pid: record.pid,
      started_at_ms: record.started_at_ms,
      log_path: record.combined_log_path,
      persistent: record.persistent === true,
    });
  }

  emitTaskEnd(record: StoredTaskRecord): void {
    const command = truncateCommand(record.command);
    const kind = this.subspawnKind(record);
    const envelope = this.buildSubspawnEnvelope(kind, record.task_id);
    const payload = {
      type: "meridian.task.end",
      ...envelope,
      task_id: record.task_id,
      subspawn_id: record.task_id,
      wait_policy: record.wait_policy,
      command,
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
      status: record.status,
      exit_code: record.exit_code,
      success: record.success,
      signal: record.signal,
      duration_ms: record.duration_ms,
      log_path: record.combined_log_path,
      persistent: record.persistent === true,
    });
  }

  emitTaskPing(record: StoredTaskRecord): void {
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

  lifecycleJobSnapshot(record: StoredTaskRecord): Record<string, unknown> {
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
}

export function defaultEffectivePingIntervalMs(
  record: StoredTaskRecord,
  defaults: SpawnTaskPingDefaults,
): number {
  return resolveEffectivePingIntervalMs(record.ping_interval_ms, defaults.pingIntervalMs);
}
