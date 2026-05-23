import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { matchesKey } from "@earendil-works/pi-tui";

import { backgroundForegroundBash } from "../background_foreground";
import {
  getForegroundUserBashTaskId,
  setOnForegroundBashChange,
  USER_BASH_FOREGROUND_HINT,
} from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";

const FOREGROUND_BASH_STATUS_KEY = "meridian-background-tasks:foreground-bash";

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

function syncForegroundStatus(ctx: ExtensionContext | null): void {
  if (!ctx?.hasUI) {
    return;
  }
  const hasForeground = getForegroundUserBashTaskId() != null;
  ctx.ui.setStatus(
    FOREGROUND_BASH_STATUS_KEY,
    hasForeground ? USER_BASH_FOREGROUND_HINT : undefined,
  );
}

export function setupForegroundBackgroundShortcut(
  pi: ExtensionAPI,
  getPanelHost: () => TaskPanelHost | null,
): void {
  let unsubTerminalInput: (() => void) | null = null;
  let activeCtx: ExtensionContext | null = null;

  setOnForegroundBashChange(() => {
    syncForegroundStatus(activeCtx);
  });

  pi.on("session_start", async (_event, ctx) => {
    unsubTerminalInput?.();
    unsubTerminalInput = null;
    activeCtx = ctx.hasUI ? ctx : null;
    syncForegroundStatus(activeCtx);

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
    syncForegroundStatus(activeCtx);
    activeCtx = null;
  });
}

/** Clear foreground UI listener when extension unloads (tests). */
export function teardownForegroundBackgroundShortcut(): void {
  setOnForegroundBashChange(null);
}
