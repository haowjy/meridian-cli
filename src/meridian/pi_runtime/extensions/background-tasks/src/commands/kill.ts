import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import type { TaskPanelHost } from "../panel/host";
import type { DockActions } from "../hooks/widget";
import { killPsRow, listUnifiedRows, pickHint, resolveTargetRow } from "./actions";
import type { TaskRegistry } from "../task_registry";
import type { BackgroundTaskRecord } from "../types";

export function registerPsKillCommand(
  pi: ExtensionAPI,
  panelHost: TaskPanelHost,
  dockActions: DockActions,
  host: {
    getRegistry: () => TaskRegistry | null;
    mergeRows: (tasks: BackgroundTaskRecord[]) => import("../types").PsRow[];
  },
): void {
  pi.registerCommand("ps:kill", {
    description: "Kill a running task or cancel a spawn",
    handler: async (args, ctx) => {
      const registry = host.getRegistry();
      if (!registry) {
        if (ctx.ui?.notify) {
          ctx.ui.notify("registry unavailable", "warning");
        }
        return;
      }
      const rows = await listUnifiedRows(registry, host.mergeRows, true);
      const target = resolveTargetRow(rows, args);
      if (!target) {
        if (ctx.ui?.notify) {
          ctx.ui.notify(pickHint(rows, "kill"), "warning");
        }
        return;
      }
      const result = await killPsRow(registry, target);
      if (result.ok && target.kind !== "meridian_spawn") {
        const id = target.task_id;
        if (dockActions.getFocusedProcessId() === id) {
          dockActions.setFocus(null);
        }
      }
      void panelHost.list();
      if (ctx.ui?.notify) {
        ctx.ui.notify(result.message, result.ok ? "info" : "warning");
      }
    },
  });
}
