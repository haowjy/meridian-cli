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

export type SpawnStateFile = {
  id: string;
  parent_id?: string | null;
  model?: string | null;
  agent?: string | null;
  status: string;
  started_at?: string | null;
  terminal: {
    status: string;
    exit_code: number;
    finished_at: string;
    published_at: string;
    duration_secs?: number | null;
    total_cost_usd?: number | null;
  } | null;
  originating_bash_id?: string | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOptionalString(value: unknown): value is string | null | undefined {
  return value == null || typeof value === "string";
}

function parseTerminalFacts(
  value: unknown,
): NonNullable<SpawnStateFile["terminal"]> | undefined {
  if (!isRecord(value)) return undefined;
  if (
    typeof value.status !== "string" ||
    typeof value.exit_code !== "number" ||
    !Number.isFinite(value.exit_code) ||
    typeof value.finished_at !== "string" ||
    typeof value.published_at !== "string" ||
    (value.duration_secs != null && (typeof value.duration_secs !== "number" || !Number.isFinite(value.duration_secs))) ||
    (value.total_cost_usd != null && (typeof value.total_cost_usd !== "number" || !Number.isFinite(value.total_cost_usd)))
  ) {
    return undefined;
  }
  return {
    status: value.status,
    exit_code: value.exit_code,
    finished_at: value.finished_at,
    published_at: value.published_at,
    duration_secs: value.duration_secs as number | null | undefined,
    total_cost_usd: value.total_cost_usd as number | null | undefined,
  };
}

export function parseSpawnStateFile(value: unknown): SpawnStateFile | null {
  if (!isRecord(value) || typeof value.id !== "string") return null;
  const status = typeof value.status === "string" ? value.status : "unknown";
  const base: SpawnStateFile = {
    id: value.id,
    status,
    terminal: null,
    parent_id: isOptionalString(value.parent_id) ? value.parent_id : null,
    model: isOptionalString(value.model) ? value.model : null,
    agent: isOptionalString(value.agent) ? value.agent : null,
    started_at: isOptionalString(value.started_at) ? value.started_at : null,
    originating_bash_id: isOptionalString(value.originating_bash_id)
      ? value.originating_bash_id
      : null,
  };
  if (value.terminal == null) return base;
  const terminal = parseTerminalFacts(value.terminal);
  if (terminal === undefined || terminal.status !== status) {
    return { ...base, status: "unknown" };
  }
  return { ...base, terminal };
}

export function isTerminalBashStatus(status: string | undefined | null): boolean {
  return status === "exited" || status === "killed" || status === "timed_out";
}
