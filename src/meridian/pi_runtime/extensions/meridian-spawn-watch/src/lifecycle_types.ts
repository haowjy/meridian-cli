export type WaitPolicy = "tracked" | "detached";
export type SubspawnKind = "bash" | "meridian_spawn";

export type InternalSubspawnEvent = {
  subspawn_id?: string;
  wait_policy?: WaitPolicy;
  kind?: SubspawnKind;
  command?: string;
  command_is_meridian_spawn?: boolean;
  status?: string;
  success?: boolean;
  reason?: string;
  log_path?: string;
  exit_code?: unknown;
  signal?: unknown;
  pid?: unknown;
  persistent?: boolean;
};

export type ToolContentPart = {
  type?: string;
  text?: string;
  [key: string]: unknown;
};

export type ToolResultEvent = {
  toolName?: string;
  content?: ToolContentPart[];
  input?: {
    wait_policy?: WaitPolicy;
    job_id?: string;
    command?: string;
  };
  details?: {
    state?: string;
    wait_policy?: WaitPolicy;
    job_id?: string;
    pid?: number;
    command?: string;
    stdout_tail?: string;
    stderr_tail?: string;
    log_tail?: string;
    text?: string;
    message?: string;
    output?: string;
    persistent?: boolean;
    job?: {
      job_id?: string;
      wait_policy?: WaitPolicy;
      status?: string;
      command?: string;
      persistent?: boolean;
    };
    jobs?: Array<{
      job_id?: string;
      wait_policy?: WaitPolicy;
      status?: string;
      command?: string;
    }>;
    found?: boolean;
    [key: string]: unknown;
  };
  isError?: boolean;
  [key: string]: unknown;
};

export type ChildState = {
  kind: "bash" | "meridian_spawn";
  waitPolicy: WaitPolicy;
  persistent: boolean;
  startedAtMs: number;
  pid: number | null;
};

export type NotificationState = {
  id: string;
  queuedAtMs: number;
  delivered: boolean;
};

export type ChildOutcomeStatus = "succeeded" | "failed" | "cancelled" | "timed_out";

export type ChildOutcome = {
  subspawn_id: string;
  status: ChildOutcomeStatus;
  success: boolean;
  reason?: string;
};

export type ActiveWaveState = {
  id: string;
  startedAtMs: number;
  deadlineAtMs: number;
  deadlineTimer: NodeJS.Timeout | null;
  trackedChildIds: Set<string>;
  outcomes: Map<string, ChildOutcome>;
};

export type CommandResult = {
  stdout: string;
  stderr: string;
  exitCode: number | null;
  signal: string | null;
  spawnError: string | null;
  timedOut: boolean;
};
