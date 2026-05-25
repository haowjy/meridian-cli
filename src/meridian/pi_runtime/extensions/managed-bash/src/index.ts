import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { renderTable } from "../../shared/ui";

export default function managedBashExtension(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "bash",
    label: "Bash",
    description: "Run a shell command. Meridian-managed bash supports background execution and bash_manage follow-up actions.",
    promptSnippet: "Run shell commands; use background=true for long-running work and bash_manage to inspect it.",
    parameters: Type.Object({
      command: Type.String(),
      timeout_min: Type.Optional(Type.Number({ minimum: 1, maximum: 59 })),
      background: Type.Optional(Type.Boolean()),
    }),
    async execute(_toolCallId, params) {
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify({ stub: true, tool: "bash", params }, null, 2),
          },
        ],
        details: { stub: true, tool: "bash", params },
      };
    },
  });

  pi.registerTool({
    name: "bash_manage",
    label: "Bash Manage",
    description: "List, inspect, wait for, kill, or detach Meridian-managed background bash tasks.",
    promptSnippet: "Manage background bash tasks with actions list, output, kill, wait, and detach.",
    parameters: Type.Object({
      action: Type.Union([
        Type.Literal("list"),
        Type.Literal("output"),
        Type.Literal("kill"),
        Type.Literal("wait"),
        Type.Literal("detach"),
      ]),
      bash_id: Type.Optional(Type.String()),
      include_completed: Type.Optional(Type.Boolean()),
      timeout_min: Type.Optional(Type.Number({ minimum: 1, maximum: 59 })),
    }),
    async execute(_toolCallId, params) {
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify({ stub: true, tool: "bash_manage", params }, null, 2),
          },
        ],
        details: { stub: true, tool: "bash_manage", params },
      };
    },
  });

  pi.registerCommand("ps", {
    description: "List Meridian-managed bash tasks for this Pi session.",
    handler: async (_args, ctx) => {
      const lines = renderTable(
        [
          { header: "ID", width: 10, render: () => "-" },
          { header: "STATE", width: 12, render: () => "stub" },
          { header: "COMMAND", width: 40, render: () => "managed-bash scaffold loaded" },
        ],
        [{}],
        80,
      );
      ctx.ui.notify(lines.join("\n"), "info");
    },
  });

  for (const name of ["ps:b", "ps:kill", "ps:logs"] as const) {
    pi.registerCommand(name, {
      description: `${name} scaffold for Meridian-managed bash tasks.`,
      handler: async (_args, ctx) => {
        ctx.ui.notify(`${name}: managed-bash scaffold loaded`, "info");
      },
    });
  }
}
