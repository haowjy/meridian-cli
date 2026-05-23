import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import type { TaskPanelHost } from "../panel/host";

export function registerPsClearCommand(pi: ExtensionAPI, panelHost: TaskPanelHost): void {
  pi.registerCommand("ps:clear", {
    description: "Clear finished tasks from the list",
    handler: async (_args, ctx) => {
      const cleared = await panelHost.clearFinished();
      if (ctx.ui?.notify) {
        ctx.ui.notify(`cleared ${cleared} finished task(s)`);
      }
    },
  });
}
