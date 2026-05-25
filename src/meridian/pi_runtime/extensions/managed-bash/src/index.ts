import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { Key, matchesKey } from "@earendil-works/pi-tui";

import { renderTable } from "../../shared/ui";
import { BashRuntime, type BashManageParams, type BashParams } from "./bash_runtime";

function formatToolResult(result: unknown): string {
  if (!result || typeof result !== "object") return String(result ?? "");
  const obj = result as Record<string, unknown>;

  if (typeof obj.error === "string") return `Error: ${obj.error}`;

  if ("stdout" in obj || "stderr" in obj) {
    const stdout = typeof obj.stdout === "string" ? obj.stdout : "";
    const stderr = typeof obj.stderr === "string" ? obj.stderr : "";
    return stdout + stderr;
  }

  if (typeof obj.output === "string") return obj.output;
  if (typeof obj.message === "string") return obj.message;

  if (Array.isArray(obj.rows)) return formatRows(obj.rows);

  if (typeof obj.bash_id === "string" && typeof obj.status === "string") {
    return `${obj.bash_id}: ${obj.status}`;
  }

  return String(result);
}

function formatRows(rows: unknown[]): string {
  const objects = rows.filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === "object");
  if (objects.length === 0) return "No managed bash tasks.";
  return renderTable(
    [
      { header: "ID", width: 10, render: (row: Record<string, unknown>) => String(row.bash_id ?? "") },
      { header: "STATE", width: 12, render: (row: Record<string, unknown>) => String(row.status ?? "") },
      { header: "DUR", width: 8, render: (row: Record<string, unknown>) => `${Math.floor(Number(row.duration_secs ?? 0))}s` },
      { header: "COMMAND", width: 60, render: (row: Record<string, unknown>) => String(row.command ?? "") },
    ],
    objects,
    100,
  ).join("\n");
}

export default function managedBashExtension(pi: ExtensionAPI): void {
  const runtime = new BashRuntime();

  pi.on?.("session_shutdown", async () => {
    await runtime.shutdown();
  });

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
      const result = await runtime.execute(params, signal);
      return {
        content: [{ type: "text", text: formatToolResult(result) }],
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
    async execute(_toolCallId, params: BashManageParams) {
      const result = await runtime.manage(params);
      return {
        content: [{ type: "text", text: formatToolResult(result) }],
        details: result,
      };
    },
  });

  pi.registerCommand("ps", {
    description: "List Meridian-managed bash tasks for this Pi session.",
    handler: async (_args, ctx) => {
      const result = (await runtime.manage({ action: "list", include_completed: true })) as {
        rows?: Array<Record<string, unknown>>;
      };
      const rows = result.rows ?? [];
      const columns = [
        { header: "ID", width: 10, render: (row: Record<string, unknown>) => String(row.bash_id ?? "") },
        { header: "STATE", width: 12, render: (row: Record<string, unknown>) => String(row.status ?? "") },
        { header: "DUR", width: 8, render: (row: Record<string, unknown>) => `${Math.floor(Number(row.duration_secs ?? 0))}s` },
        { header: "COMMAND", width: 48, render: (row: Record<string, unknown>) => String(row.command ?? "") },
      ];
      await ctx.ui.custom(
        (_tui, theme, _keybindings, done) => ({
          render(width: number): string[] {
            const title = theme.fg("accent", theme.bold(" Meridian /ps — managed bash "));
            const body = rows.length ? renderTable(columns, rows, Math.max(20, width - 4)) : ["No Meridian-managed bash tasks."];
            return [
              theme.fg("border", "╭" + "─".repeat(Math.max(0, width - 2)) + "╮"),
              theme.fg("border", "│ ") + title,
              theme.fg("border", "├" + "─".repeat(Math.max(0, width - 2)) + "┤"),
              ...body.map((line) => theme.fg("border", "│ ") + line),
              theme.fg("border", "├" + "─".repeat(Math.max(0, width - 2)) + "┤"),
              theme.fg("dim", "│ q/esc close • /ps:logs <id> • /ps:kill <id>"),
              theme.fg("border", "╰" + "─".repeat(Math.max(0, width - 2)) + "╯"),
            ];
          },
          handleInput(data: string): void {
            if (matchesKey(data, Key.escape) || data === "q") done(undefined);
          },
          invalidate(): void {},
        }),
        { overlay: true, overlayOptions: { width: "90%", maxHeight: "80%" } },
      );
    },
  });

  pi.registerCommand("ps:kill", {
    description: "Kill a Meridian-managed bash task.",
    handler: async (args, ctx) => {
      const result = await runtime.manage({ action: "kill", bash_id: args.trim() });
      ctx.ui.notify(JSON.stringify(result, null, 2), "info");
    },
  });

  pi.registerCommand("ps:logs", {
    description: "Show a Meridian-managed bash task log tail.",
    handler: async (args, ctx) => {
      const result = await runtime.manage({ action: "output", bash_id: args.trim() });
      ctx.ui.notify(JSON.stringify(result, null, 2), "info");
    },
  });

  pi.registerCommand("ps:b", {
    description: "Foreground-to-background promotion is automatic via bash timeout_min.",
    handler: async (_args, ctx) => {
      ctx.ui.notify("Use bash({ timeout_min }) to auto-background long-running foreground commands.", "info");
    },
  });
}
