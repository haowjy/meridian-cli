import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { matchesKey } from "@earendil-works/pi-tui";

import { backgroundForegroundBash } from "../background_foreground";
import { getForegroundUserBashTaskId, USER_BASH_PANEL_BACKGROUND_MSG } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";

export const CTRL_B_CHORD_WINDOW_MS = 450;

export type CtrlBChordAction = "ignore" | "arm" | "background";

export type CtrlBChordStep = {
  action: CtrlBChordAction;
  consume: boolean;
  nextLastCtrlBAtMs: number | null;
};

/** Pure double Ctrl+B detector — only active when a foreground `$` bash task exists. */
export function stepCtrlBBackgroundChord(
  data: string,
  nowMs: number,
  lastCtrlBAtMs: number | null,
  hasForeground: boolean,
  windowMs: number = CTRL_B_CHORD_WINDOW_MS,
): CtrlBChordStep {
  if (!matchesKey(data, "ctrl+b")) {
    return { action: "ignore", consume: false, nextLastCtrlBAtMs: lastCtrlBAtMs };
  }
  if (!hasForeground) {
    return { action: "ignore", consume: false, nextLastCtrlBAtMs: null };
  }
  if (lastCtrlBAtMs != null && nowMs - lastCtrlBAtMs <= windowMs) {
    return { action: "background", consume: true, nextLastCtrlBAtMs: null };
  }
  return { action: "arm", consume: true, nextLastCtrlBAtMs: nowMs };
}

export function setupForegroundBackgroundShortcut(
  pi: ExtensionAPI,
  getPanelHost: () => TaskPanelHost | null,
): void {
  let unsubTerminalInput: (() => void) | null = null;
  let lastCtrlBAtMs: number | null = null;

  pi.on("session_start", async (_event, ctx) => {
    unsubTerminalInput?.();
    unsubTerminalInput = null;
    lastCtrlBAtMs = null;

    if (!ctx.hasUI) {
      return;
    }

    unsubTerminalInput = ctx.ui.onTerminalInput((data) => {
      const step = stepCtrlBBackgroundChord(
        data,
        Date.now(),
        lastCtrlBAtMs,
        getForegroundUserBashTaskId() != null,
      );
      lastCtrlBAtMs = step.nextLastCtrlBAtMs;

      if (step.action === "ignore") {
        return undefined;
      }
      if (step.action === "arm") {
        return { consume: true };
      }

      const host = getPanelHost();
      if (host) {
        void backgroundForegroundBash(host).then((result) => {
          if (result.ok) {
            ctx.ui.notify(USER_BASH_PANEL_BACKGROUND_MSG, "info");
          }
        });
      }
      return { consume: true };
    });
  });

  pi.on("session_shutdown", async () => {
    unsubTerminalInput?.();
    unsubTerminalInput = null;
    lastCtrlBAtMs = null;
  });
}
