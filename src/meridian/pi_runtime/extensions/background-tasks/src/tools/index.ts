import { Type } from "typebox";

import type { ExtensionAPI } from "../../types";
import type { BackgroundTaskRecord, BackgroundTaskAction } from "../types";
import {
  DEFAULT_BG_READ_BYTES,
  DEFAULT_BG_WAIT_TIMEOUT_MS,
  MAX_BG_READ_BYTES,
  MAX_BG_WAIT_TIMEOUT_MS,
  TaskRegistry,
  clamp,
  normalizeWaitPolicy,
  sessionIdFromContext,
  toInt,
  type ToolContext,
} from "../task_registry";
import type { PsRow } from "../types";

export type BackgroundTaskToolHost = {
  getRegistry: () => TaskRegistry | null;
  getSessionId: () => string;
  setSession: (sessionId: string, registry: TaskRegistry) => void;
  createRegistry: (sessionId: string) => TaskRegistry;
  mergeRows: (tasks: BackgroundTaskRecord[]) => PsRow[];
};

const ACTIONS: BackgroundTaskAction[] = [
  "start",
  "list",
  "output",
  "logs",
  "wait",
  "cancel",
  "clear",
];

export function setupBackgroundTaskTool(pi: ExtensionAPI, host: BackgroundTaskToolHost): void {
  if (!pi.registerTool) {
    return;
  }

  pi.registerTool({
    name: "background_task",
    label: "background_task",
    description:
      "Manage background OS tasks. Actions: start, list, output, logs, wait, cancel, clear.",
    parameters: Type.Object({
      action: Type.Union(ACTIONS.map((action) => Type.Literal(action))),
      task_id: Type.Optional(Type.String()),
      command: Type.Optional(Type.String()),
      label: Type.Optional(Type.String()),
      cwd: Type.Optional(Type.String()),
      wait_policy: Type.Optional(Type.Union([Type.Literal("tracked"), Type.Literal("detached")])),
      include_completed: Type.Optional(Type.Boolean()),
      max_bytes: Type.Optional(Type.Number({ minimum: 1, maximum: MAX_BG_READ_BYTES })),
      offset: Type.Optional(Type.Number({ minimum: 0 })),
      timeout_ms: Type.Optional(Type.Number({ minimum: 1, maximum: MAX_BG_WAIT_TIMEOUT_MS })),
      ping_interval_ms: Type.Optional(Type.Number({ minimum: 1 })),
      persistent: Type.Optional(Type.Boolean()),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, context) {
      const action = (params as { action?: BackgroundTaskAction }).action;
      const registry = await resolveRegistry(host, context);

      if (registry == null) {
        return errorResult("background_task registry unavailable");
      }

      switch (action) {
        case "start":
          return startAction(registry, params, context);
        case "list":
          return listAction(registry, host, params);
        case "output":
          return outputAction(registry, params);
        case "logs":
          return logsAction(registry, params);
        case "wait":
          return waitAction(registry, params);
        case "cancel":
          return cancelAction(registry, params);
        case "clear":
          return clearAction(registry);
        default:
          return errorResult(`Unknown action '${String(action)}'`);
      }
    },
  });
}

async function resolveRegistry(
  host: BackgroundTaskToolHost,
  context: unknown,
): Promise<TaskRegistry | null> {
  const sessionId = sessionIdFromContext(context as ToolContext, host.getSessionId());
  if (sessionId !== host.getSessionId()) {
    const created = host.createRegistry(sessionId);
    await created.initialize();
    await host.getRegistry()?.shutdownCleanup();
    host.setSession(sessionId, created);
  }
  return host.getRegistry();
}

function lifecycleTaskDetails(
  task: BackgroundTaskRecord,
  extras: Record<string, unknown> = {},
): Record<string, unknown> {
  const state = task.status === "running" ? "running" : "exited";
  return {
    job_id: task.task_id,
    task_id: task.task_id,
    state,
    wait_policy: task.wait_policy,
    pid: task.pid,
    job: {
      job_id: task.task_id,
      task_id: task.task_id,
      wait_policy: task.wait_policy,
      pid: task.pid,
      status: task.status,
      command: task.command,
      persistent: task.persistent === true,
    },
    ...extras,
  };
}

function errorResult(message: string): {
  content: Array<{ type: string; text: string }>;
  details: Record<string, unknown>;
  isError: boolean;
} {
  return {
    content: [{ type: "text", text: message }],
    details: { success: false, message },
    isError: true,
  };
}

async function startAction(
  registry: TaskRegistry,
  params: Record<string, unknown>,
  context: unknown,
): Promise<Record<string, unknown>> {
  const command = String(params.command ?? "").trim();
  if (!command) {
    return errorResult("start requires command");
  }
  const waitPolicy = normalizeWaitPolicy(params.wait_policy);
  const cwd =
    typeof params.cwd === "string" && params.cwd.length > 0
      ? params.cwd
      : (context as ToolContext).cwd ?? process.cwd();
  const env = { ...process.env } as Record<string, string>;
  const label = typeof params.label === "string" ? params.label : undefined;
  const pingIntervalMs =
    typeof params.ping_interval_ms === "number" && params.ping_interval_ms > 0
      ? Math.trunc(params.ping_interval_ms)
      : undefined;
  const persistent = params.persistent === true;

  const { runtimeJob } = await registry.startJob(command, waitPolicy, cwd, env, label, {
    pingIntervalMs,
    persistent,
  });
  const taskId = runtimeJob.record.task_id;
  await registry.detachJob(taskId);
  const task = (await registry.getTask(taskId)) ?? { task_id: taskId };

  return {
    content: [
      {
        type: "text",
        text: `Started background task ${taskId}.`,
      },
    ],
    details: {
      action: "start",
      success: true,
      task,
      tasks: [task],
      jobs: [task],
      ...lifecycleTaskDetails(task),
    },
  };
}

async function listAction(
  registry: TaskRegistry,
  host: BackgroundTaskToolHost,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const includeCompleted = params.include_completed === true;
  const tasks = await registry.list(includeCompleted);
  const rows = host.mergeRows(tasks);
  const lines =
    rows.length === 0
      ? "No background tasks or spawns."
      : rows
          .map((row) => {
            if (row.kind === "meridian_spawn") {
              return `${row.spawn_id} spawn ${row.status}`;
            }
            const spawn = row.meridian_spawn;
            if (spawn) {
              return `${row.task_id} spawn ${spawn.spawn_id} ${spawn.status} ${row.label}`;
            }
            return `${row.task_id} ${row.kind} ${row.status} ${row.label}`;
          })
          .join("\n");
  return {
    content: [{ type: "text", text: lines }],
    details: { action: "list", success: true, tasks, rows, jobs: tasks },
  };
}

async function outputAction(
  registry: TaskRegistry,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const taskId = String(params.task_id ?? "");
  if (!taskId) {
    return errorResult("output requires task_id");
  }
  const maxBytes = clamp(toInt(params.max_bytes, DEFAULT_BG_READ_BYTES), 1, MAX_BG_READ_BYTES);
  const result = await registry.readLog(taskId, maxBytes, params.offset as number | undefined);
  if (result == null) {
    return errorResult(`Task ${taskId} not found`);
  }
  return {
    content: [{ type: "text", text: result.data }],
    details: { action: "output", success: true, task_id: taskId, ...result },
  };
}

async function logsAction(
  registry: TaskRegistry,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const taskId = String(params.task_id ?? "");
  if (!taskId) {
    return errorResult("logs requires task_id");
  }
  const jobs = await registry.list(true);
  const task = jobs.find((t) => t.task_id === taskId);
  if (!task) {
    return errorResult(`Task ${taskId} not found`);
  }
  return {
    content: [
      {
        type: "text",
        text: `combined: ${task.combined_log_path}`,
      },
    ],
    details: {
      action: "logs",
      success: true,
      task_id: taskId,
      combined_log_path: task.combined_log_path,
      stdout_log_path: task.stdout_log_path,
      stderr_log_path: task.stderr_log_path,
    },
  };
}

async function waitAction(
  registry: TaskRegistry,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const taskId = String(params.task_id ?? "");
  if (!taskId) {
    return errorResult("wait requires task_id");
  }
  const timeoutMs = clamp(
    toInt(params.timeout_ms, DEFAULT_BG_WAIT_TIMEOUT_MS),
    1,
    MAX_BG_WAIT_TIMEOUT_MS,
  );
  const record = await registry.waitForCompletion(taskId, timeoutMs);
  if (record == null) {
    return errorResult(`Task ${taskId} not found`);
  }
  if (record.status === "running") {
    return {
      content: [{ type: "text", text: `Task ${taskId} is still running.` }],
      details: {
        action: "wait",
        success: true,
        task: record,
        ...lifecycleTaskDetails(record),
      },
    };
  }
  const log = await registry.readLog(taskId, DEFAULT_BG_READ_BYTES);
  return {
    content: [{ type: "text", text: log?.data ?? "" }],
    details: {
      action: "wait",
      success: record.success === true,
      state: "exited",
      task: record,
      log_tail: log?.data ?? "",
    },
    isError: record.success === false,
  };
}

async function cancelAction(
  registry: TaskRegistry,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const taskId = String(params.task_id ?? "");
  if (!taskId) {
    return errorResult("cancel requires task_id");
  }
  const record = await registry.killJob(taskId);
  if (record == null) {
    return errorResult(`Task ${taskId} not found`);
  }
  return {
    content: [{ type: "text", text: `Task ${taskId} terminated.` }],
    details: {
      action: "cancel",
      success: true,
      task: record,
      found: true,
      ...lifecycleTaskDetails(record),
    },
  };
}

async function clearAction(registry: TaskRegistry): Promise<Record<string, unknown>> {
  const removed = await registry.clearFinished();
  return {
    content: [{ type: "text", text: `Cleared ${removed} finished task(s).` }],
    details: { action: "clear", success: true, removed },
  };
}
