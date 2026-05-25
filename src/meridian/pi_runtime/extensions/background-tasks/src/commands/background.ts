import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { backgroundForegroundBash } from "../background_foreground";
import { USER_BASH_PANEL_BACKGROUND_MSG } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";

type PsNotifyLevel = "info" | "warning";

export function psBackgroundSubcommandMessage(result: {
  ok: boolean;
  reason?: string;
}): { level: PsNotifyLevel; message: string } {
  if (result.ok) {
    return { level: "info", message: USER_BASH_PANEL_BACKGROUND_MSG };
  }
  if (result.reason === "no_foreground") {
    return { level: "warning", message: "No foreground $ task to background" };
  }
  return {
    level: "warning",
    message: result.reason
      ? `Could not background task: ${result.reason}`
      : "Could not background task",
  };
}

function notifyPs(
  ctx: { ui?: { notify?: (msg: string, level?: string) => void } },
  level: PsNotifyLevel,
  message: string,
): void {
  if (ctx.ui?.notify) {
    ctx.ui.notify(message, level);
  } else {
    process.stdout.write(`${message}\n`);
  }
}

export async function runPsBackgroundForeground(
  panelHost: TaskPanelHost,
  ctx: { ui?: { notify?: (msg: string, level?: string) => void } },
): Promise<void> {
  const result = await backgroundForegroundBash(panelHost);
  const { level, message } = psBackgroundSubcommandMessage(result);
  notifyPs(ctx, level, message);
}

export function registerPsBackgroundCommands(
  pi: ExtensionAPI,
  panelHost: TaskPanelHost,
): void {
  const handler = async (
    _args: string,
    ctx: { ui?: { notify?: (msg: string, level?: string) => void } },
  ): Promise<void> => {
    await runPsBackgroundForeground(panelHost, ctx);
  };

  pi.registerCommand("ps:b", {
    description: "Background the foreground $ task (short)",
    handler,
  });

  pi.registerCommand("ps:background", {
    description: "Background the foreground $ task",
    handler,
  });
}
