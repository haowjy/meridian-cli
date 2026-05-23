import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import type { TaskPanelHost } from "../panel/host";
import type { DockActions } from "../hooks/widget";

export function registerPsPinCommand(pi: ExtensionAPI, panelHost: TaskPanelHost, dockActions: DockActions): void {
  pi.registerCommand("ps:pin", {
    description: "Pin the log dock to a task or spawn id",
    handler: async (args, ctx) => {
      const id = args.trim();
      if (!id) {
        const entries = await panelHost.list();
        const live = entries.find((entry) => entry.isLive);
        if (!live) {
          if (ctx.ui?.notify) {
            ctx.ui.notify("pin requires task_id or spawn_id", "warning");
          }
          return;
        }
        dockActions.setFocus(live.id);
        dockActions.expand();
        return;
      }
      const entry = await panelHost.get(id);
      if (!entry) {
        if (ctx.ui?.notify) {
          ctx.ui.notify(`unknown id: ${id}`, "warning");
        }
        return;
      }
      dockActions.setFocus(entry.id);
      dockActions.expand();
    },
  });
}
