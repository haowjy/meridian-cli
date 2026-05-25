import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { renderTable } from "../../shared/ui";

export default function meridianSpawnWatchExtension(pi: ExtensionAPI): void {
  pi.registerCommand("mspawn", {
    description: "List Meridian spawns correlated to this Pi session.",
    handler: async (_args, ctx) => {
      const lines = renderTable(
        [
          { header: "ID", width: 10, render: () => "-" },
          { header: "STATUS", width: 12, render: () => "stub" },
          { header: "REPORT", width: 40, render: () => "meridian-spawn-watch scaffold loaded" },
        ],
        [{}],
        80,
      );
      ctx.ui.notify(lines.join("\n"), "info");
    },
  });

  for (const name of ["mspawn:show", "mspawn:wait", "mspawn:cancel", "mspawn:log"] as const) {
    pi.registerCommand(name, {
      description: `${name} scaffold for Meridian spawn watch.`,
      handler: async (_args, ctx) => {
        ctx.ui.notify(`${name}: meridian-spawn-watch scaffold loaded`, "info");
      },
    });
  }
}
