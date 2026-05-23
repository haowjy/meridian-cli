import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { LogOverlayComponent } from "../components/log-overlay-component";
import { PsPanelFrame } from "../components/ps-panel-frame";
import { formatPsTable } from "../format_rows";
import { refreshStatusWidget, STATUS_WIDGET_ID } from "../hooks/widget";
import type { TaskPanelHost } from "../panel/host";
import type { DockActions } from "../hooks/widget";
import { listUnifiedRows } from "./actions";
import type { TaskRegistry } from "../task_registry";
import type { BackgroundTaskRecord, PsRow } from "../types";

/** Full-screen /ps takeover (covers editor + widgets; chat may remain above in Pi). */
const PS_PANEL_OPTIONS = {
  overlay: true as const,
  overlayOptions: {
    width: "100%",
    maxHeight: "100%",
  },
};

const LOG_OVERLAY_OPTIONS = {
  overlay: true as const,
  overlayOptions: {
    width: "90%",
    maxHeight: "80%",
    anchor: "center" as const,
  },
};

export function registerPsCommand(
  pi: ExtensionAPI,
  panelHost: TaskPanelHost,
  _dockActions: DockActions,
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

      ctx.ui.setWidget(STATUS_WIDGET_ID, undefined);
      try {
        await ctx.ui.custom<string | null>(
          (tui, theme, _keybindings, done) => {
            let frame: PsPanelFrame | null = null;
            const actions = {
              onQuit: (): void => {
                frame?.dispose();
                frame = null;
                done(null);
              },
              onOpenStream: async (taskId: string): Promise<void> => {
                await ctx.ui.custom<null>(
                  (overlayTui, overlayTheme, _overlayKb, overlayDone) =>
                    new LogOverlayComponent({
                      tui: overlayTui,
                      theme: overlayTheme,
                      host: panelHost,
                      initialProcessId: taskId,
                      streamFollow: true,
                      done: () => overlayDone(null),
                    }),
                  LOG_OVERLAY_OPTIONS,
                );
                frame?.invalidate();
                tui.requestRender();
              },
            };
            frame = new PsPanelFrame(tui, theme, actions, panelHost);
            return frame;
          },
          PS_PANEL_OPTIONS,
        );
      } finally {
        refreshStatusWidget();
      }
    },
  });
}
