import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { formatSettingsPingSummary } from "../hooks/widget";

export function registerPsSettingsCommand(pi: ExtensionAPI): void {
  pi.registerCommand("ps:settings", {
    description: "Background task extension settings and ping policy",
    handler: async (_args, ctx) => {
      const message = [
        "State: MERIDIAN_PI_STATE_DIR/background-tasks/<session>/tasks/",
        "",
        "Task ping policy:",
        formatSettingsPingSummary(),
      ].join("\n");
      if (ctx.ui?.notify) {
        ctx.ui.notify(message);
        return;
      }
      process.stdout.write(`${message}\n`);
    },
  });
}
