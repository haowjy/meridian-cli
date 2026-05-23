import type { TaskRegistry } from "./task_registry";

export const USER_BASH_PANEL_BACKGROUND_MSG = "Sent to background — /ps";

export type BashBridgeState = {
  registry: TaskRegistry | null;
};

type ToolResultEvent = {
  toolName?: string;
  details?: Record<string, unknown>;
  input?: Record<string, unknown>;
};

type UserBashEvent = {
  type?: string;
  command: string;
  cwd: string;
  excludeFromContext?: boolean;
};

type BashExecOptions = {
  onData?: (data: Buffer) => void;
  signal?: AbortSignal;
  timeout?: number;
  env?: NodeJS.ProcessEnv;
};

type PiWithHooks = {
  on?: (event: string, handler: (...args: unknown[]) => unknown) => void;
};

type AsyncBashDetails = {
  state?: string;
  jobId?: string;
  type?: string;
};

function readAsyncDetails(details: Record<string, unknown>): AsyncBashDetails | null {
  const asyncBlock = details.async;
  if (!asyncBlock || typeof asyncBlock !== "object") {
    return null;
  }
  return asyncBlock as AsyncBashDetails;
}

function jobIdFrom(event: ToolResultEvent): string | null {
  const details = event.details ?? {};
  const asyncBlock = readAsyncDetails(details);
  const job = details.job as { job_id?: string; task_id?: string } | undefined;
  const input = event.input;
  return (
    asyncBlock?.jobId ||
    (typeof details.job_id === "string" ? details.job_id : null) ||
    (typeof details.task_id === "string" ? details.task_id : null) ||
    job?.job_id ||
    job?.task_id ||
    (typeof input?.job_id === "string" ? input.job_id : null) ||
    (typeof input?.task_id === "string" ? input.task_id : null) ||
    null
  );
}

function commandFrom(event: ToolResultEvent): string {
  const details = event.details ?? {};
  const input = event.input;
  if (typeof details.command === "string") {
    return details.command;
  }
  if (typeof input?.command === "string") {
    return input.command;
  }
  const job = details.job as { command?: string } | undefined;
  if (typeof job?.command === "string") {
    return job.command;
  }
  return "";
}

/** Trailing shell `&` on interactive `$` commands (quote-aware; not agent bash). */
export function splitUserBashBackground(command: string): {
  background: boolean;
  execCommand: string;
} {
  const trimmed = command.trim();
  if (!trimmed || trimmed === "&") {
    return { background: false, execCommand: trimmed };
  }

  let inSingle = false;
  let inDouble = false;
  let escape = false;
  let lastUnquotedAmpersand = -1;

  for (let i = 0; i < trimmed.length; i++) {
    const ch = trimmed[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (ch === "\\" && (inSingle || inDouble)) {
      escape = true;
      continue;
    }
    if (!inDouble && ch === "'") {
      inSingle = !inSingle;
      continue;
    }
    if (!inSingle && ch === '"') {
      inDouble = !inDouble;
      continue;
    }
    if (!inSingle && !inDouble && ch === "&") {
      lastUnquotedAmpersand = i;
    }
  }

  if (lastUnquotedAmpersand < 0) {
    return { background: false, execCommand: trimmed };
  }

  const afterAmpersand = trimmed.slice(lastUnquotedAmpersand + 1);
  if (afterAmpersand.trim() !== "") {
    return { background: false, execCommand: trimmed };
  }

  const execCommand = trimmed.slice(0, lastUnquotedAmpersand).trimEnd();
  if (!execCommand) {
    return { background: false, execCommand: trimmed };
  }

  return { background: true, execCommand };
}

let foregroundUserBashTaskId: string | null = null;
const foregroundBashChangeListeners = new Set<() => void>();
let onAgentBashRunning: ((taskId: string, waitPolicy: unknown) => void) | null = null;

/** Task id blocking Pi's interactive `$` slot, if any. */
export function getForegroundUserBashTaskId(): string | null {
  return foregroundUserBashTaskId;
}

export function onForegroundBashChange(handler: () => void): () => void {
  foregroundBashChangeListeners.add(handler);
  return () => {
    foregroundBashChangeListeners.delete(handler);
  };
}

/** Clear foreground listeners between tests or extension teardown. */
export function clearForegroundBashChangeListeners(): void {
  foregroundBashChangeListeners.clear();
}

export function setOnAgentBashRunning(
  handler: ((taskId: string, waitPolicy: unknown) => void) | null,
): void {
  onAgentBashRunning = handler;
}

export function setForegroundUserBashTaskId(taskId: string | null): void {
  const previous = foregroundUserBashTaskId;
  foregroundUserBashTaskId = taskId;
  if (previous !== taskId) {
    for (const listener of foregroundBashChangeListeners) {
      listener();
    }
  }
}

function bashLifecycleState(details: Record<string, unknown>): string | null {
  const asyncState = readAsyncDetails(details)?.state;
  if (typeof asyncState === "string") {
    return asyncState;
  }
  if (typeof details.state === "string") {
    return details.state;
  }
  return null;
}

async function syncRunningFromToolResult(
  registry: TaskRegistry,
  event: ToolResultEvent,
  jobId: string,
): Promise<void> {
  const details = event.details ?? {};
  const waitPolicy = details.wait_policy;
  await registry.syncBashToolRunning({
    taskId: jobId,
    command: commandFrom(event),
    pid: typeof details.pid === "number" ? details.pid : null,
    waitPolicy,
    cwd: typeof details.cwd === "string" ? details.cwd : undefined,
    logPath: typeof details.log_path === "string" ? details.log_path : undefined,
    pingIntervalMs:
      typeof details.ping_interval_ms === "number" ? details.ping_interval_ms : undefined,
    persistent: details.persistent === true,
  });
  if (waitPolicy !== "detached") {
    onAgentBashRunning?.(jobId, waitPolicy);
  }
}

async function syncExitedFromToolResult(
  registry: TaskRegistry,
  event: ToolResultEvent,
  jobId: string,
): Promise<void> {
  const details = event.details ?? {};
  await registry.syncBashToolExited({
    taskId: jobId,
    exitCode: typeof details.exit_code === "number" ? details.exit_code : null,
    signal:
      typeof details.signal === "string" || typeof details.signal === "number"
        ? details.signal
        : null,
  });
}

/** Register Pi `bash` tool_result and `$` user_bash into the unified TaskRegistry. */
export function setupBashBridge(pi: PiWithHooks, state: BashBridgeState): void {
  pi.on?.("user_bash", async (event: unknown) => {
    const registry = state.registry;
    if (registry == null) {
      return;
    }
    const typed = event as UserBashEvent;
    const command = typed.command?.trim();
    if (!command) {
      return;
    }
    const cwd = typed.cwd?.trim() || process.cwd();
    const { background, execCommand } = splitUserBashBackground(command);

    if (background) {
      const env = { ...process.env } as Record<string, string>;
      const { runtimeJob } = await registry.startJob(
        execCommand,
        "detached",
        cwd,
        env,
        undefined,
        { ingress: "bash" },
      );
      await registry.detachJob(runtimeJob.record.task_id);
      const taskId = runtimeJob.record.task_id;
      return {
        result: {
          exitCode: 0,
          output: `Detached task ${taskId} — /ps to manage\n`,
          cancelled: false,
        },
      };
    }

    return {
      operations: {
        exec: async (
          execCommand: string,
          execCwd: string,
          options: BashExecOptions,
        ): Promise<{ exitCode: number | null }> => {
          const env = {
            ...process.env,
            ...(options.env ?? {}),
          } as Record<string, string>;
          const timeoutMs = Math.max(
            1,
            Math.trunc((options.timeout ?? 300) * 1000),
          );

          const taskId = (
            await registry.startJob(
              execCommand,
              "detached",
              execCwd,
              env,
              undefined,
              {
                ingress: "bash",
                onChunk: (chunk) => {
                  options.onData?.(chunk);
                },
              },
            )
          ).runtimeJob.record.task_id;
          await registry.detachJob(taskId);

          const abortListener = (): void => {
            void registry.killJob(taskId);
          };
          if (options.signal) {
            if (options.signal.aborted) {
              abortListener();
            } else {
              options.signal.addEventListener("abort", abortListener, { once: true });
            }
          }

          setForegroundUserBashTaskId(taskId);
          try {
            const done = await registry.waitForCompletion(taskId, timeoutMs);
            if (
              done?.status === "running" &&
              getForegroundUserBashTaskId() === null
            ) {
              options.onData?.(
                Buffer.from(`${USER_BASH_PANEL_BACKGROUND_MSG}\n`, "utf-8"),
              );
              return { exitCode: 0 };
            }
            return { exitCode: done?.exit_code ?? null };
          } finally {
            if (foregroundUserBashTaskId === taskId) {
              setForegroundUserBashTaskId(null);
            }
            if (options.signal) {
              options.signal.removeEventListener("abort", abortListener);
            }
          }
        },
      },
    };
  });

  pi.on?.("tool_execution_update", async (event: unknown) => {
    const typed = event as ToolResultEvent & { toolCallId?: string };
    if (typed.toolName !== "bash") {
      return;
    }
    const registry = state.registry;
    if (registry == null) {
      return;
    }
    const jobId = jobIdFrom(typed);
    if (jobId == null) {
      return;
    }
    const details = typed.details ?? {};
    if (bashLifecycleState(details) === "running") {
      await syncRunningFromToolResult(registry, typed, jobId);
    }
  });

  pi.on?.("tool_result", async (event: unknown) => {
    const typed = event as ToolResultEvent;
    if (typed.toolName !== "bash") {
      return;
    }
    const registry = state.registry;
    if (registry == null) {
      return;
    }

    const jobId = jobIdFrom(typed);
    if (jobId == null) {
      return;
    }

    const details = typed.details ?? {};
    const stateValue = bashLifecycleState(details);

    if (stateValue === "running") {
      await syncRunningFromToolResult(registry, typed, jobId);
      return;
    }

    if (
      stateValue === "exited" ||
      stateValue === "completed" ||
      stateValue === "failed"
    ) {
      await syncExitedFromToolResult(registry, typed, jobId);
    }
  });
}
