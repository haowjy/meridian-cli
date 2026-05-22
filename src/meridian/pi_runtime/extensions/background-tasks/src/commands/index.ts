import type { ExtensionAPI } from "../../types";
import type { BackgroundTaskRecord } from "../types";
import type { TaskRegistry } from "../task_registry";
import type { PsRow } from "../types";
import { formatPsTable } from "../format_rows";
import {
  killPsRow,
  listUnifiedRows,
  pickHint,
  readPsRowLogs,
  resolveTargetRow,
} from "./actions";

type CommandCtx = {
  hasUI?: boolean;
  ui?: { notify?: (msg: string, level?: string) => void };
};

type PiWithCommands = ExtensionAPI & {
  registerCommand?: (
    name: string,
    spec: {
      description: string;
      handler: (args: string, ctx: CommandCtx) => Promise<void>;
    },
  ) => void;
};

function notify(ctx: CommandCtx, message: string, level = "info"): void {
  const text = message.slice(0, 8000);
  if (ctx.hasUI !== false && ctx.ui?.notify) {
    ctx.ui.notify(text, level);
    return;
  }
  process.stdout.write(`${text}\n`);
}

export function setupPsCommands(
  pi: PiWithCommands,
  host: {
    getRegistry: () => TaskRegistry | null;
    mergeRows: (tasks: BackgroundTaskRecord[]) => PsRow[];
  },
): void {
  if (!pi.registerCommand) {
    return;
  }

  const getRows = async (): Promise<PsRow[] | null> => {
    const registry = host.getRegistry();
    if (!registry) {
      return null;
    }
    return listUnifiedRows(registry, host.mergeRows, true);
  };

  pi.registerCommand("ps", {
    description: "List background tasks and Meridian spawns",
    handler: async (args, ctx) => {
      const rows = await getRows();
      if (rows == null) {
        notify(ctx, "background-tasks registry unavailable", "warning");
        return;
      }
      const arg = args.trim();
      if (arg === "json") {
        notify(ctx, JSON.stringify(rows, null, 2));
        return;
      }
      notify(ctx, formatPsTable(rows));
    },
  });

  const register = (
    name: string,
    description: string,
    handler: (args: string, ctx: CommandCtx) => Promise<void>,
  ): void => {
    pi.registerCommand?.(`ps:${name}`, { description, handler });
  };

  register("clear", "Clear finished tasks from the list", async (_args, ctx) => {
    const registry = host.getRegistry();
    if (!registry) {
      notify(ctx, "registry unavailable", "warning");
      return;
    }
    const n = await registry.clearFinished();
    notify(ctx, `cleared ${n} finished task(s)`);
  });

  register("kill", "Kill a running task or cancel a spawn", async (args, ctx) => {
    const registry = host.getRegistry();
    if (!registry) {
      notify(ctx, "registry unavailable", "warning");
      return;
    }
    const rows = await listUnifiedRows(registry, host.mergeRows, true);
    const target = resolveTargetRow(rows, args);
    if (!target) {
      notify(ctx, pickHint(rows, "kill"), "warning");
      return;
    }
    const result = await killPsRow(registry, target);
    notify(ctx, result.message, result.ok ? "info" : "warning");
  });

  register("logs", "Show combined log tail for a task or spawn", async (args, ctx) => {
    const registry = host.getRegistry();
    if (!registry) {
      notify(ctx, "registry unavailable", "warning");
      return;
    }
    const rows = await listUnifiedRows(registry, host.mergeRows, true);
    const target = resolveTargetRow(rows, args) ?? rows[rows.length - 1];
    if (!target) {
      notify(ctx, "No rows. Pass task_id or spawn_id.", "warning");
      return;
    }
    const result = await readPsRowLogs(registry, target);
    notify(ctx, result.message, result.ok ? "info" : "warning");
  });

  register("pin", "Pin dock to a process (TUI when pi-tui is available)", async (args, ctx) => {
    const rows = await getRows();
    if (!rows) {
      notify(ctx, "registry unavailable", "warning");
      return;
    }
    const id = args.trim() || rows.find((r) => r.kind !== "meridian_spawn")?.task_id;
    if (!id) {
      notify(ctx, "pin requires task_id", "warning");
      return;
    }
    notify(
      ctx,
      `Dock pin reserved for pi-tui panel (task ${id}). Use /ps:logs ${id} for log tail.`,
    );
  });

  register("dock", "Show or hide log dock (TUI when pi-tui is available)", async (args, ctx) => {
    const mode = args.trim().toLowerCase() || "toggle";
    notify(
      ctx,
      `Dock ${mode} requires pi-tui ProcessesComponent (not bundled). Logs: /ps:logs <id>.`,
    );
  });

  register("settings", "Background task extension settings", async (_args, ctx) => {
    notify(
      ctx,
      "background-tasks settings: state under MERIDIAN_PI_STATE_DIR/background-tasks/<session>/tasks/",
    );
  });
}
