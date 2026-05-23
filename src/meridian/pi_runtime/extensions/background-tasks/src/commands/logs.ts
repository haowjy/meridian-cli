import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { LogOverlayComponent } from "../components/log-overlay-component";
import type { TaskPanelHost } from "../panel/host";

export function registerPsLogsCommand(pi: ExtensionAPI, panelHost: TaskPanelHost): void {
  pi.registerCommand("ps:logs", {
    description: "Open log viewer for a task (search, scroll, stream filter)",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) {
        return;
      }
      const processId = args.trim() || undefined;
      await ctx.ui.custom<null>(
        (tui, theme, _kb, done) =>
          new LogOverlayComponent({
            tui,
            theme,
            host: panelHost,
            initialProcessId: processId,
            done: () => done(null),
          }),
        {
          overlay: true,
          overlayOptions: {
            width: "90%",
            maxHeight: "80%",
            anchor: "center",
          },
        },
      );
    },
  });
}
