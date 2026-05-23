import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { PsPanelFrame } from "../components/ps-panel-frame";
import { formatPsTable } from "../format_rows";
import type { TaskPanelHost } from "../panel/host";
import type { DockActions } from "../hooks/widget";
import { listUnifiedRows } from "./actions";
import type { TaskRegistry } from "../task_registry";
import type { BackgroundTaskRecord, PsRow } from "../types";

export function registerPsCommand(
  pi: ExtensionAPI,
  panelHost: TaskPanelHost,
  dockActions: DockActions,
  host: {
    getRegistry: () => TaskRegistry | null;
    mergeRows: (tasks: BackgroundTaskRecord[]) => PsRow[];
  },
): void {
  pi.registerCommand("ps", {
    description: "View and manage background tasks and spawns",
    handler: async (args, ctx) => {
      const trimmed = args.trim();
      const registry = host.getRegistry();
      if (!registry) {
        const message = "background-tasks registry unavailable";
        if (ctx.ui?.notify) {
          ctx.ui.notify(message, "warning");
        } else {
          process.stdout.write(`${message}\n`);
        }
        return;
      }

      if (trimmed === "json") {
        const rows = await listUnifiedRows(registry, host.mergeRows, true);
        const text = JSON.stringify(rows, null, 2);
        if (ctx.ui?.notify) {
          ctx.ui.notify(text);
        } else {
          process.stdout.write(`${text}\n`);
        }
        return;
      }

      if (!ctx.hasUI) {
        const rows = await listUnifiedRows(registry, host.mergeRows, true);
        const text = formatPsTable(rows);
        if (ctx.ui?.notify) {
          ctx.ui.notify(text);
        } else {
          process.stdout.write(`${text}\n`);
        }
        return;
      }

      const result = await ctx.ui.custom<string | null>((tui, theme, _keybindings, done) => {
        const onClose = (taskId?: string): void => {
          if (taskId) {
            dockActions.setFocus(taskId);
          }
          done(taskId ?? null);
        };
        return new PsPanelFrame(tui, theme, onClose, panelHost);
      });

      if (result) {
        dockActions.setFocus(result);
      }
    },
  });
}
