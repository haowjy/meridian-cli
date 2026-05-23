import type { TaskRegistry } from "./task_registry";

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
  await registry.syncBashToolRunning({
    taskId: jobId,
    command: commandFrom(event),
    pid: typeof details.pid === "number" ? details.pid : null,
    waitPolicy: details.wait_policy,
    cwd: typeof details.cwd === "string" ? details.cwd : undefined,
    logPath: typeof details.log_path === "string" ? details.log_path : undefined,
    pingIntervalMs:
      typeof details.ping_interval_ms === "number" ? details.ping_interval_ms : undefined,
    persistent: details.persistent === true,
  });
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
export function setupBashBridge(
  pi: PiWithHooks,
  state: { registry: TaskRegistry | null },
): void {
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

          const { runtimeJob } = await registry.startJob(
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
          );
          await registry.detachJob(runtimeJob.record.task_id);

          const abortListener = (): void => {
            void registry.killJob(runtimeJob.record.task_id);
          };
          if (options.signal) {
            if (options.signal.aborted) {
              abortListener();
            } else {
              options.signal.addEventListener("abort", abortListener, { once: true });
            }
          }

          try {
            const done = await registry.waitForCompletion(
              runtimeJob.record.task_id,
              timeoutMs,
            );
            return { exitCode: done?.exit_code ?? null };
          } finally {
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
