import { randomBytes } from "node:crypto";
import { spawn } from "node:child_process";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { renderTable } from "../../shared/ui";

type BashParams = {
  command: string;
  timeout_min?: number;
  background?: boolean;
};

type BashResult = {
  stdout: string;
  stderr: string;
  exit_code: number;
};

const OUTPUT_CAP_BYTES = 50 * 1024;

function makeBashId(): string {
  return `b-${randomBytes(4).toString("hex")}`;
}

function appendCapped(current: string, chunk: string): string {
  const next = current + chunk;
  if (Buffer.byteLength(next, "utf-8") <= OUTPUT_CAP_BYTES) {
    return next;
  }
  return next.slice(Math.max(0, next.length - OUTPUT_CAP_BYTES));
}

async function runForegroundBash(
  params: BashParams,
  signal: AbortSignal | undefined,
): Promise<BashResult> {
  const bashId = makeBashId();
  const timeoutMin = params.timeout_min ?? 55;
  const child = spawn(params.command, {
    cwd: process.cwd(),
    env: { ...process.env, MERIDIAN_PI_BASH_ID: bashId },
    shell: true,
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stdout = "";
  let stderr = "";

  child.stdout?.setEncoding("utf-8");
  child.stdout?.on("data", (chunk: string | Buffer) => {
    stdout = appendCapped(stdout, typeof chunk === "string" ? chunk : chunk.toString("utf-8"));
  });
  child.stderr?.setEncoding("utf-8");
  child.stderr?.on("data", (chunk: string | Buffer) => {
    stderr = appendCapped(stderr, typeof chunk === "string" ? chunk : chunk.toString("utf-8"));
  });

  return await new Promise<BashResult>((resolve) => {
    let settled = false;
    const finish = (exitCode: number, note?: string): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
      resolve({
        stdout,
        stderr: note ? appendCapped(stderr, note) : stderr,
        exit_code: exitCode,
      });
    };
    const kill = (): void => {
      try {
        child.kill("SIGTERM");
      } catch {
        // ignore process-race failures
      }
    };
    const abort = (): void => {
      kill();
      finish(-1, "\n[command aborted]\n");
    };
    const timeout = setTimeout(() => {
      kill();
      finish(-1, `\n[command exceeded timeout_min=${timeoutMin}; full background promotion is not implemented yet]\n`);
    }, Math.max(1, timeoutMin) * 60_000);

    if (signal?.aborted) {
      abort();
      return;
    }
    signal?.addEventListener("abort", abort, { once: true });
    child.once("error", (error) => finish(-1, `\n[failed to start command: ${error.message}]\n`));
    child.once("close", (code) => finish(code ?? -1));
  });
}

function startDetachedBash(params: BashParams): { bash_id: string; status: "started" } {
  const bashId = makeBashId();
  const child = spawn(params.command, {
    cwd: process.cwd(),
    detached: true,
    env: { ...process.env, MERIDIAN_PI_BASH_ID: bashId },
    shell: true,
    stdio: "ignore",
  });
  child.unref();
  return { bash_id: bashId, status: "started" };
}

export default function managedBashExtension(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "bash",
    label: "Bash",
    description: "Run a shell command. Meridian-managed bash supports background execution and bash_manage follow-up actions.",
    promptSnippet: "Run shell commands; use background=true for long-running work and bash_manage to inspect it.",
    parameters: Type.Object({
      command: Type.String(),
      timeout_min: Type.Optional(Type.Number({ minimum: 1, maximum: 59 })),
      background: Type.Optional(Type.Boolean()),
    }),
    async execute(_toolCallId, params: BashParams, signal) {
      const result = params.background
        ? startDetachedBash(params)
        : await runForegroundBash(params, signal);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
        details: result,
      };
    },
  });

  pi.registerTool({
    name: "bash_manage",
    label: "Bash Manage",
    description: "List, inspect, wait for, kill, or detach Meridian-managed background bash tasks.",
    promptSnippet: "Manage background bash tasks with actions list, output, kill, wait, and detach.",
    parameters: Type.Object({
      action: Type.Union([
        Type.Literal("list"),
        Type.Literal("output"),
        Type.Literal("kill"),
        Type.Literal("wait"),
        Type.Literal("detach"),
      ]),
      bash_id: Type.Optional(Type.String()),
      include_completed: Type.Optional(Type.Boolean()),
      timeout_min: Type.Optional(Type.Number({ minimum: 1, maximum: 59 })),
    }),
    async execute(_toolCallId, params) {
      const result = {
        rows: [],
        message: "bash_manage registry is not implemented yet; foreground bash execution is available.",
        params,
      };
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
        details: result,
      };
    },
  });

  pi.registerCommand("ps", {
    description: "List Meridian-managed bash tasks for this Pi session.",
    handler: async (_args, ctx) => {
      const lines = renderTable(
        [
          { header: "ID", width: 10, render: () => "-" },
          { header: "STATE", width: 12, render: () => "stub" },
          { header: "COMMAND", width: 40, render: () => "bash registry not implemented yet" },
        ],
        [{}],
        80,
      );
      ctx.ui.notify(lines.join("\n"), "info");
    },
  });

  for (const name of ["ps:b", "ps:kill", "ps:logs"] as const) {
    pi.registerCommand(name, {
      description: `${name} scaffold for Meridian-managed bash tasks.`,
      handler: async (_args, ctx) => {
        ctx.ui.notify(`${name}: bash registry is not implemented yet`, "info");
      },
    });
  }
}
