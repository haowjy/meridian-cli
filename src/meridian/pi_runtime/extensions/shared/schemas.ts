export type BashStatus = "running" | "exited" | "killed" | "timed_out";
export type SpawnStatus = "running" | "succeeded" | "failed" | "cancelled" | "timed_out" | "unknown";

export type BashRecord = {
  bash_id: string;
  command: string;
  cwd: string;
  pid: number | null;
  status: BashStatus;
  is_background: boolean;
  is_tracked: boolean;
  exit_code: number | null;
  started_at_ms: number;
  ended_at_ms: number | null;
  log_path: string;
  stdout_log_path: string;
  stderr_log_path: string;
  log_bytes: number;
  timeout_min: number;
  originating_bash_id: string | null;
};

export type BashRecordsFile = {
  v: 1;
  spawn_id: string;
  updated_at_ms: number;
  records: Record<string, BashRecord>;
};

export type LastNotificationFile = {
  ts_epoch_secs: number;
  notified_spawn_ids: string[];
};

export type ObservedSpawnsFile = {
  v: 1;
  spawn_id: string;
  updated_at_ms: number;
  observed_spawn_ids: string[];
  waiting_spawn_ids?: string[];
};

export type SpawnStateFile = {
  id: string;
  parent_id?: string | null;
  model?: string | null;
  agent?: string | null;
  status?: SpawnStatus | string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_secs?: number | null;
  total_cost_usd?: number | null;
  originating_bash_id?: string | null;
};

export function isTerminalBashStatus(status: string | undefined | null): boolean {
  return status === "exited" || status === "killed" || status === "timed_out";
}

export function isTerminalSpawnStatus(status: string | undefined | null): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled" || status === "timed_out";
}
