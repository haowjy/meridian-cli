export type TaskStatus = "running" | "exited" | "failed" | "killed" | "timed_out";
export type WaitPolicy = "tracked" | "detached";

export type TaskIngress = "background_task" | "bash";

export type BackgroundTaskRecord = {
  task_id: string;
  label: string;
  command: string;
  cwd: string;
  pid: number | null;
  wait_policy: WaitPolicy;
  status: TaskStatus;
  success: boolean | null;
  exit_code: number | null;
  signal: string | number | null;
  started_at_ms: number;
  ended_at_ms: number | null;
  stdout_log_path: string;
  stderr_log_path: string;
  combined_log_path: string;
  log_bytes: number;
  log_truncated: boolean;
  ingress?: TaskIngress;
  persistent?: boolean;
  ping_interval_ms?: number | null;
  last_activity_at_ms?: number | null;
  next_ping_at_ms?: number | null;
};

export type MeridianSpawnAttachment = {
  spawn_id: string;
  status: string;
  summary?: string;
};

export type PsRowKind = "process" | "meridian_spawn";

export type PsRow =
  | ({ kind: "process" } & BackgroundTaskRecord & {
      /** Confirmed spawn launched from this bash task (`Spawn id:` in log). */
      meridian_spawn?: MeridianSpawnAttachment;
    })
  | {
      kind: "meridian_spawn";
      spawn_id: string;
      task_id?: string;
      status: string;
      summary?: string;
    };

export type BackgroundTaskAction =
  | "start"
  | "list"
  | "output"
  | "logs"
  | "wait"
  | "cancel"
  | "clear";
