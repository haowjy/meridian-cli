import type { BackgroundTaskRecord, TaskIngress, TaskStatus, WaitPolicy } from "./types";

export const MAX_COMMAND_LENGTH = 512;
export const MAX_FOREGROUND_TAIL_BYTES = 16 * 1024;
export const DEFAULT_BG_READ_BYTES = 8 * 1024;
export const MAX_BG_READ_BYTES = 64 * 1024;
export const MAX_LOG_BYTES = 10 * 1024 * 1024;
export const DEFAULT_BG_WAIT_TIMEOUT_MS = 30_000;
export const MAX_BG_WAIT_TIMEOUT_MS = 10 * 60 * 1000;
export const TASK_START_EVENT = "meridian:task:start";
export const TASK_END_EVENT = "meridian:task:end";
export const TASK_PING_EVENT = "meridian:task:ping";
export const TASK_OUTPUT_EVENT = "meridian:task:output";
export const TASK_OUTPUT_THROTTLE_MS = 100;
export const SUBSPAWN_START_EVENT = "meridian:subspawn:start";
export const SUBSPAWN_END_EVENT = "meridian:subspawn:end";
export const PING_SCAN_INTERVAL_MS = 60_000;

/** Internal record shape persisted to meta.json (superset of BackgroundTaskRecord). */
export type StoredTaskRecord = BackgroundTaskRecord & {
  emitted_start: boolean;
  duration_ms: number | null;
  log_path: string;
  ingress: TaskIngress;
};

export type { TaskStatus, WaitPolicy };
