import { readFileSync } from "node:fs";

import type { ExtensionAPI, Theme } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import {
  openSelectablePanel,
  openTextOverlay,
  type PanelCommandContext,
  type SelectablePanelColumn,
} from "../../shared/selectable_panel";
import { formatDurationSecs, renderTable } from "../../shared/ui";
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
      { header: "DUR", width: 8, render: (row: Record<string, unknown>) => formatDurationSecs(Number(row.duration_secs ?? 0)) },
      { header: "COMMAND", width: 60, render: (row: Record<string, unknown>) => String(row.command ?? "") },
    ],
    objects,
    100,
  ).join("\n");
}

type BashPanelRow = {
  bash_id: string;
  command: string;
  cwd: string;
  status: string;
  is_background: boolean;
  is_tracked: boolean;
  exit_code: number | null;
  duration_secs: number;
  log_path: string;
  log_bytes: number;
};

function toBashPanelRows(result: unknown): BashPanelRow[] {
  const rows = (result as { rows?: unknown[] } | null)?.rows ?? [];
  return rows.filter((row): row is BashPanelRow => {
    const obj = row as Partial<BashPanelRow> | null;
    return Boolean(obj) && typeof obj === "object" && typeof obj.bash_id === "string";
  });
}

function tailFile(filePath: string, maxBytes = 4096): string {
  try {
    const text = readFileSync(filePath, "utf-8");
    return text.slice(Math.max(0, text.length - maxBytes));
  } catch {
    return "";
  }
}

function renderBashPreview(row: BashPanelRow, theme: Theme): string[] {
  const dim = (value: string) => theme.fg("dim", value);
  const status = row.status === "running" ? theme.fg("success", row.status) : dim(row.status);
  const output = tailFile(row.log_path, 2048).trimEnd();
  const lines = output ? output.split(/\r?\n/).slice(-3) : [dim("(no output yet)")];
  return [
    `${theme.fg("accent", row.bash_id)} ${status} ${dim(formatDurationSecs(row.duration_secs))}`,
    dim(row.command),
    ...lines,
  ];
}

const BASH_PANEL_COLUMNS: SelectablePanelColumn<BashPanelRow>[] = [
  { header: "ID", width: 10, render: (row) => row.bash_id },
  { header: "STATE", width: 10, render: (row) => row.status },
  { header: "BG", width: 3, render: (row) => (row.is_background ? "yes" : "no") },
  { header: "DUR", width: 8, render: (row) => formatDurationSecs(row.duration_secs), align: "right" },
  { header: "SIZE", width: 8, render: (row) => `${row.log_bytes}B`, align: "right" },
  { header: "COMMAND", width: 56, render: (row) => row.command },
];

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
      const loadRows = async (): Promise<BashPanelRow[]> =>
        toBashPanelRows(await runtime.manage({ action: "list", include_completed: true }));

      if (ctx.hasUI === false || !ctx.ui?.custom) {
        process.stdout.write(`${formatRows(await loadRows())}\n`);
        return;
      }

      await openSelectablePanel(ctx as PanelCommandContext, {
        title: "Meridian /ps — managed bash",
        columns: BASH_PANEL_COLUMNS,
        loadRows,
        getRowId: (row) => row.bash_id,
        renderPreview: renderBashPreview,
        emptyMessage: "No Meridian-managed bash tasks.",
        footer: "enter logs · j/k select · r refresh · q close",
        onEnter: async (row) => {
          await openTextOverlay(ctx as PanelCommandContext, {
            title: `Bash log ${row.bash_id}`,
            footer: "r refresh · q close",
            loadText: async () => {
              const result = await runtime.manage({ action: "output", bash_id: row.bash_id });
              return String((result as { output?: unknown }).output ?? formatToolResult(result));
            },
          });
        },
      });
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
      const bashId = args.trim();
      const loadText = async (): Promise<string> => {
        const result = await runtime.manage({ action: "output", bash_id: bashId });
        return String((result as { output?: unknown }).output ?? formatToolResult(result));
      };
      if (ctx.hasUI !== false && ctx.ui?.custom) {
        await openTextOverlay(ctx as PanelCommandContext, {
          title: `Bash log ${bashId}`,
          loadText,
        });
        return;
      }
      process.stdout.write(`${await loadText()}\n`);
    },
  });

  pi.registerCommand("ps:b", {
    description: "Foreground-to-background promotion is automatic via bash timeout_min.",
    handler: async (_args, ctx) => {
      ctx.ui.notify("Use bash({ timeout_min }) to auto-background long-running foreground commands.", "info");
    },
  });
}
