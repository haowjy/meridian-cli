import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { matchesKey } from "@earendil-works/pi-tui";

import { backgroundForegroundBash } from "../background_foreground";
import { getForegroundUserBashTaskId } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";

export type CtrlBBackgroundStep = {
  action: "ignore" | "background";
  consume: boolean;
};

/** Single Ctrl+B backgrounds foreground `$` bash (Claude Code parity). */
export function stepCtrlBBackground(
  data: string,
  hasForeground: boolean,
): CtrlBBackgroundStep {
  if (!matchesKey(data, "ctrl+b")) {
    return { action: "ignore", consume: false };
  }
  if (!hasForeground) {
    return { action: "ignore", consume: false };
  }
  return { action: "background", consume: true };
}

export function setupForegroundBackgroundShortcut(
  pi: ExtensionAPI,
  getPanelHost: () => TaskPanelHost | null,
): void {
  let unsubTerminalInput: (() => void) | null = null;

  pi.on("session_start", async (_event, ctx) => {
    unsubTerminalInput?.();
    unsubTerminalInput = null;

    if (!ctx.hasUI) {
      return;
    }

    unsubTerminalInput = ctx.ui.onTerminalInput((data) => {
      const step = stepCtrlBBackground(data, getForegroundUserBashTaskId() != null);

      if (step.action === "ignore") {
        return undefined;
      }

      const host = getPanelHost();
      if (host) {
        void backgroundForegroundBash(host);
      }
      return { consume: true };
    });
  });

  pi.on("session_shutdown", async () => {
    unsubTerminalInput?.();
    unsubTerminalInput = null;
  });
}
