import { Type, type Static } from "typebox";
import { Value } from "typebox/value";

export type BashStatus = "running" | "exited" | "killed" | "timed_out";

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
  ping_sent_at_ms?: number | null;
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

export const SpawnStateFileSchema = Type.Object({
  id: Type.String(),
  parent_id: Type.Optional(Type.Union([Type.String(), Type.Null()])),
  model: Type.Optional(Type.Union([Type.String(), Type.Null()])),
  agent: Type.Optional(Type.Union([Type.String(), Type.Null()])),
  status: Type.String(),
  started_at: Type.Optional(Type.Union([Type.String(), Type.Null()])),
  terminal: Type.Union([
    Type.Object({
      // Terminal status lives ONLY at the top level; `terminal` presence is
      // the completion discriminant. Vocabulary is owned by Meridian, not Pi.
      exit_code: Type.Number(),
      finished_at: Type.String(),
      published_at: Type.String(),
      duration_secs: Type.Optional(Type.Union([Type.Number(), Type.Null()])),
      total_cost_usd: Type.Optional(Type.Union([Type.Number(), Type.Null()])),
    }),
    Type.Null(),
  ]),
  originating_bash_id: Type.Optional(Type.Union([Type.String(), Type.Null()])),
});

export type SpawnStateFile = Static<typeof SpawnStateFileSchema>;

export function parseSpawnStateFile(value: unknown): SpawnStateFile | null {
  return Value.Check(SpawnStateFileSchema, value) ? value : null;
}

export function isTerminalBashStatus(status: string | undefined | null): boolean {
  return status === "exited" || status === "killed" || status === "timed_out";
}
