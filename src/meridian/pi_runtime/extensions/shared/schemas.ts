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
  notification_consumed_at_ms?: number | null;
  execution_error?: string;
};

export type BashRecordsFile = {
  v: 1;
  spawn_id: string;
  updated_at_ms: number;
  records: Record<string, BashRecord>;
  runtime_error?: string;
};

/** Consumed only by an explicit operation or admission of this exact custom message. */
export type DeliveryReceiptsFile = {
  v: 1;
  spawn_id: string;
  messages: Record<string, string[]>;
};

export const BashRecordsFileSchema = Type.Object({
  v: Type.Literal(1),
  spawn_id: Type.String(),
  updated_at_ms: Type.Number(),
  runtime_error: Type.Optional(Type.String()),
  records: Type.Record(Type.String(), Type.Object({
    bash_id: Type.String(), command: Type.String(), cwd: Type.String(),
    pid: Type.Union([Type.Integer({ minimum: 1 }), Type.Null()]),
    status: Type.Union([Type.Literal("running"), Type.Literal("exited"), Type.Literal("killed"), Type.Literal("timed_out")]),
    is_background: Type.Boolean(), is_tracked: Type.Boolean(),
    exit_code: Type.Union([Type.Integer(), Type.Null()]),
    started_at_ms: Type.Number(), ended_at_ms: Type.Union([Type.Number(), Type.Null()]),
    log_path: Type.String(), stdout_log_path: Type.String(), stderr_log_path: Type.String(),
    log_bytes: Type.Integer({ minimum: 0 }), timeout_min: Type.Number(),
    originating_bash_id: Type.Union([Type.String(), Type.Null()]),
    ping_sent_at_ms: Type.Optional(Type.Union([Type.Number(), Type.Null()])),
    notification_consumed_at_ms: Type.Optional(Type.Union([Type.Number(), Type.Null()])),
    execution_error: Type.Optional(Type.String()),
  })),
});

export function parseBashRecordsFile(value: unknown): BashRecordsFile | null {
  if (!Value.Check(BashRecordsFileSchema, value)) return null;
  if (!Number.isFinite(value.updated_at_ms)) return null;
  for (const [id, record] of Object.entries(value.records)) {
    if (record.bash_id !== id) return null;
    if ([record.started_at_ms, record.ended_at_ms, record.ping_sent_at_ms, record.timeout_min,
      record.notification_consumed_at_ms].some((n) => n != null && !Number.isFinite(n))) return null;
  }
  return value;
}

export type ObservedSpawnsFile = {
  v: 1;
  spawn_id: string;
  updated_at_ms: number;
  observed_spawn_ids: string[];
  waiting_spawn_ids?: string[];
  wait_reservations?: Record<string, {
    owner_pid: number;
    owner_birth_epoch: number;
    expires_at_epoch: number;
    spawn_ids: string[];
  }>;
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
    }, { additionalProperties: false }),
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
