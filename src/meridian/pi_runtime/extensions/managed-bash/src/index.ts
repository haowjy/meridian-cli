import { readFileSync } from "node:fs";

import type { ExtensionAPI, MessageRenderOptions, Theme } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";

import { openLogOverlay } from "../../shared/log_overlay";
import {
  openTaskPanel,
  type PanelCommandContext,
  type SelectablePanelColumn,
} from "../../shared/selectable_panel";
import { formatDurationSecs, renderTable } from "../../shared/ui";
import { BashRuntime, type BashListRow, type BashManageParams, type BashParams } from "./bash_runtime";

const FOREGROUND_BASH_HINT_CUSTOM_TYPE = "meridian:foreground-bash-hint";
const FOREGROUND_BASH_HINT_TEXT = "/ps to manage tasks · /ps:b to run in background";

function isBashListRow(value: unknown): value is BashListRow {
  return Boolean(value) && typeof value === "object" && typeof (value as { bash_id?: unknown }).bash_id === "string";
}

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

  if (Array.isArray(obj.rows)) return formatRows(obj.rows.filter(isBashListRow));

  if (typeof obj.bash_id === "string" && typeof obj.status === "string") {
    return `${obj.bash_id}: ${obj.status}`;
  }

  return String(result);
}

function formatRows(rows: BashListRow[]): string {
  if (rows.length === 0) return "No managed bash tasks.";
  return renderTable(
    [
      { header: "ID", width: 10, render: (row: BashListRow) => row.bash_id },
      { header: "STATE", width: 12, render: (row: BashListRow) => row.status },
      { header: "DUR", width: 8, render: (row: BashListRow) => formatDurationSecs(row.duration_secs) },
      { header: "COMMAND", width: 60, render: (row: BashListRow) => row.command },
    ],
    rows,
    100,
  ).join("\n");
}

type BashPanelRow = BashListRow;

function tailFile(filePath: string, maxBytes = 4096): string {
  try {
    const text = readFileSync(filePath, "utf-8");
    return text.slice(Math.max(0, text.length - maxBytes));
  } catch {
    return "";
  }
}

type BashLogStream = "combined" | "stdout" | "stderr";

function readInspectableLog(row: BashPanelRow, stream: BashLogStream = "combined"): string {
  const filePath = stream === "stdout" ? row.stdout_log_path : stream === "stderr" ? row.stderr_log_path : row.log_path;
  return tailFile(filePath, 1024 * 1024).trimEnd() || "(no output yet)";
}

function bashLogStreams(row: BashPanelRow): Array<{ id: BashLogStream; label: string; loadText: () => Promise<string> }> {
  return [
    { id: "combined", label: "combined", loadText: async () => readInspectableLog(row, "combined") },
    { id: "stdout", label: "stdout", loadText: async () => readInspectableLog(row, "stdout") },
    { id: "stderr", label: "stderr", loadText: async () => readInspectableLog(row, "stderr") },
  ];
}

function formatBashStatus(row: BashPanelRow, theme: Theme): string {
  const dim = (value: string) => theme.fg("dim", value);
  const success = (value: string) => theme.fg("success", value);
  const error = (value: string) => theme.fg("error", value);
  const warning = (value: string) => theme.fg("warning", value);

  if (row.status === "running") return success("● running");
  if (row.status === "exited") {
    return row.exit_code === 0 ? dim("✓ exit(0)") : error(`✗ exit(${row.exit_code ?? "?"})`);
  }
  if (row.status === "killed") return warning("✗ killed");
  return error(`✗ ${row.status}`);
}

function renderBashPreview(row: BashPanelRow, theme: Theme): string[] {
  const dim = (value: string) => theme.fg("dim", value);
  const output = tailFile(row.log_path, 2048).trimEnd();
  const lines = output ? output.split(/\r?\n/).slice(-3) : [dim("(no output yet)")];
  return [
    `${theme.fg("accent", row.bash_id)} ${formatBashStatus(row, theme)} ${dim(formatDurationSecs(row.duration_secs))}`,
    dim(row.command),
    ...lines,
  ];
}

const BASH_PANEL_COLUMNS: SelectablePanelColumn<BashPanelRow>[] = [
  { header: "ID", width: 10, render: (row, theme, selected) => (theme ? (selected ? theme.fg("accent", row.bash_id) : theme.fg("dim", row.bash_id)) : row.bash_id) },
  { header: "STATE", width: 12, render: (row, theme) => (theme ? formatBashStatus(row, theme) : row.status) },
  { header: "BG", width: 3, render: (row, theme) => (theme ? (row.is_background ? theme.fg("accent", "yes") : theme.fg("dim", "no")) : row.is_background ? "yes" : "no") },
  { header: "DUR", width: 8, render: (row, theme) => (theme ? theme.fg("dim", formatDurationSecs(row.duration_secs)) : formatDurationSecs(row.duration_secs)), align: "right" },
  { header: "SIZE", width: 8, render: (row, theme) => (theme ? theme.fg("dim", `${row.log_bytes}B`) : `${row.log_bytes}B`), align: "right" },
  { header: "COMMAND", width: 56, render: (row) => row.command },
];

type PiWithForegroundHint = ExtensionAPI & {
  sendMessage?: (
    message: { customType?: string; content: string; display?: boolean; details?: Record<string, unknown> },
    options?: { triggerTurn?: boolean; excludeFromContext?: boolean },
  ) => void | Promise<void>;
  registerMessageRenderer?: <Details>(
    customType: string,
    renderer: (
      message: { content: string; details?: Details },
      options: MessageRenderOptions,
      theme: Theme,
    ) => Text,
  ) => void;
};

const hintedForegroundBashIds = new Set<string>();

function setupForegroundBashHint(pi: PiWithForegroundHint): void {
  pi.registerMessageRenderer?.<{ taskId: string; hintText: string }>(
    FOREGROUND_BASH_HINT_CUSTOM_TYPE,
    (message, _options, theme) => new Text(theme.fg("dim", message.details?.hintText ?? message.content), 0, 0),
  );
}

function postForegroundBashHint(pi: PiWithForegroundHint, bashId: string): void {
  if (hintedForegroundBashIds.has(bashId)) return;
  hintedForegroundBashIds.add(bashId);
  try {
    void pi.sendMessage?.(
      {
        customType: FOREGROUND_BASH_HINT_CUSTOM_TYPE,
        content: "",
        display: true,
        details: { taskId: bashId, hintText: FOREGROUND_BASH_HINT_TEXT },
      },
      { triggerTurn: false, excludeFromContext: true },
    );
  } catch (error) {
    if (!(error instanceof Error) || !error.message.includes("stale")) throw error;
  }
}

function notify(ctx: { ui?: { notify?: (msg: string, level?: string) => void } }, message: string, level: "info" | "warning" = "info"): void {
  if (ctx.ui?.notify) ctx.ui.notify(message, level);
  else process.stdout.write(`${message}\n`);
}

type UserBashEvent = {
  command?: string;
  cwd?: string;
};

type BashExecOptions = {
  onData?: (data: Buffer) => void;
  signal?: AbortSignal;
  timeout?: number;
  env?: NodeJS.ProcessEnv;
};

function splitUserBashBackground(command: string): { background: boolean; execCommand: string } {
  const trimmed = command.trim();
  if (!trimmed || trimmed === "&") return { background: false, execCommand: trimmed };

  let inSingle = false;
  let inDouble = false;
  let escape = false;
  let lastUnquotedAmpersand = -1;

  for (let i = 0; i < trimmed.length; i += 1) {
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
    if (!inSingle && !inDouble && ch === "&") lastUnquotedAmpersand = i;
  }

  if (lastUnquotedAmpersand < 0) return { background: false, execCommand: trimmed };
  if (trimmed.slice(lastUnquotedAmpersand + 1).trim() !== "") return { background: false, execCommand: trimmed };

  const execCommand = trimmed.slice(0, lastUnquotedAmpersand).trimEnd();
  return execCommand ? { background: true, execCommand } : { background: false, execCommand: trimmed };
}

export default function managedBashExtension(pi: ExtensionAPI): void {
  const hintPi = pi as PiWithForegroundHint;
  setupForegroundBashHint(hintPi);
  const runtime = new BashRuntime({
    onForegroundStart: (bashId) => postForegroundBashHint(hintPi, bashId),
  });

  pi.on?.("session_shutdown", async () => {
    hintedForegroundBashIds.clear();
    await runtime.shutdown();
  });

  pi.on?.("user_bash", async (event: unknown) => {
    const typed = event as UserBashEvent;
    const command = typed.command?.trim();
    if (!command) return undefined;

    const cwd = typed.cwd?.trim() || process.cwd();
    const { background, execCommand } = splitUserBashBackground(command);
    if (background) {
      const { bash_id: bashId } = await runtime.startDetachedUserBash(execCommand, cwd, process.env);
      return {
        result: {
          exitCode: 0,
          output: `Detached task ${bashId} — /ps to manage\n`,
          cancelled: false,
        },
      };
    }

    return {
      operations: {
        exec: async (
          execCommandFromPi: string,
          execCwd: string,
          options: BashExecOptions,
        ): Promise<{ exitCode: number | null }> =>
          await runtime.executeUserBash(execCommandFromPi, execCwd, options),
      },
    };
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
      const loadRows = async (): Promise<BashPanelRow[]> => runtime.list(true);

      if (ctx.hasUI === false || !ctx.ui?.custom) {
        process.stdout.write(`${formatRows(await loadRows())}\n`);
        return;
      }

      await openTaskPanel(ctx as PanelCommandContext, {
        title: "Meridian /ps — managed bash",
        columns: BASH_PANEL_COLUMNS,
        loadRows,
        getRowId: (row) => row.bash_id,
        renderPreview: renderBashPreview,
        emptyMessage: "No Meridian-managed bash tasks.",
        footer: "enter logs · j/k select · r refresh · q close",
        onEnter: async (row) => {
          await openLogOverlay(ctx as PanelCommandContext, {
            title: `Bash log ${row.bash_id}`,
            initialFollow: row.status === "running",
            streams: bashLogStreams(row),
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
        const row = runtime.list(true).find((candidate) => candidate.bash_id === bashId);
        if (row) return readInspectableLog(row);
        const result = await runtime.manage({ action: "output", bash_id: bashId });
        return "output" in result ? result.output : formatToolResult(result);
      };
      if (ctx.hasUI !== false && ctx.ui?.custom) {
        const row = runtime.list(true).find((candidate) => candidate.bash_id === bashId);
        await openLogOverlay(ctx as PanelCommandContext, {
          title: `Bash log ${bashId}`,
          initialFollow: row?.status === "running",
          streams: row ? bashLogStreams(row) : [{ id: "combined", label: "combined", loadText }],
        });
        return;
      }
      process.stdout.write(`${await loadText()}\n`);
    },
  });

  const backgroundForegroundHandler = async (_args: string, ctx: { ui?: { notify?: (msg: string, level?: string) => void } }): Promise<void> => {
    const result = await runtime.backgroundForeground();
    if (result.ok) return;
    notify(ctx, "No foreground $ task to background", "warning");
  };

  pi.registerCommand("ps:b", {
    description: "Background the foreground $ task.",
    handler: backgroundForegroundHandler,
  });

  pi.registerCommand("ps:background", {
    description: "Background the foreground $ task.",
    handler: backgroundForegroundHandler,
  });
}
