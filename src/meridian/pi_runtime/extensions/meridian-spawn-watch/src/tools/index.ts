import { Type } from "typebox";

import type { ExtensionAPI } from "../../types";
import type { MeridianEventBus } from "../../../shared/meridian_event_bus";
import type { SpawnWatchManager } from "../spawn_manager";
import { runMeridianCommand } from "../spawn_cli";

const ACTIONS = ["list", "show", "cancel", "wait", "clear"] as const;

function parseStatus(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }
  try {
    const parsed = JSON.parse(trimmed) as { status?: string };
    if (typeof parsed.status === "string") {
      return parsed.status.toLowerCase();
    }
  } catch {
    // fall through
  }
  const match = trimmed.match(/\b(running|queued|starting|finalizing|succeeded|failed|cancelled)\b/i);
  return match?.[1]?.toLowerCase() ?? null;
}

export function setupSpawnWatchTool(
  pi: ExtensionAPI,
  manager: SpawnWatchManager,
  bus: MeridianEventBus,
): void {
  if (!pi.registerTool) {
    return;
  }

  pi.registerTool({
    name: "spawn_watch",
    label: "spawn_watch",
    description: "Inspect Meridian spawns for this Pi session. Actions: list, show, cancel, wait, clear.",
    parameters: Type.Object({
      action: Type.Union(ACTIONS.map((action) => Type.Literal(action))),
      spawn_id: Type.Optional(Type.String()),
      timeout_ms: Type.Optional(Type.Number({ minimum: 1, maximum: 600_000 })),
    }),
    async execute(_toolCallId, params) {
      const action = (params as { action?: (typeof ACTIONS)[number] }).action;
      const spawnId = typeof (params as { spawn_id?: string }).spawn_id === "string"
        ? (params as { spawn_id: string }).spawn_id
        : "";

      if (action === "list") {
        const tree = await manager.tree.read();
        const lines =
          tree.nodes.length === 0
            ? "No spawns discovered."
            : tree.nodes.map((n) => `${n.spawn_id} ${n.status} ${n.kind}`).join("\n");
        return {
          content: [{ type: "text", text: lines }],
          details: { action: "list", success: true, nodes: tree.nodes },
        };
      }

      if (action === "show") {
        if (!spawnId) {
          return { content: [{ type: "text", text: "show requires spawn_id" }], details: { success: false }, isError: true };
        }
        const result = await runMeridianCommand(["spawn", "show", spawnId, "--no-report"]);
        const status = parseStatus(result.stdout) ?? "unknown";
        const file = await manager.tree.read();
        const node = file.nodes.find((n) => n.spawn_id === spawnId);
        if (node) {
          node.status = status;
          await manager.tree.write(file);
        }
        bus.emit("meridian:spawn:updated", { spawn_id: spawnId, status });
        return {
          content: [{ type: "text", text: result.stdout || result.stderr }],
          details: { action: "show", success: true, spawn_id: spawnId, status },
        };
      }

      if (action === "cancel") {
        if (!spawnId) {
          return { content: [{ type: "text", text: "cancel requires spawn_id" }], details: { success: false }, isError: true };
        }
        const result = await runMeridianCommand(["spawn", "cancel", spawnId]);
        return {
          content: [{ type: "text", text: result.stdout || result.stderr || `cancelled ${spawnId}` }],
          details: { action: "cancel", success: result.exitCode === 0, spawn_id: spawnId },
          isError: result.exitCode !== 0,
        };
      }

      if (action === "wait") {
        if (!spawnId) {
          return { content: [{ type: "text", text: "wait requires spawn_id" }], details: { success: false }, isError: true };
        }
        const timeoutMs = typeof (params as { timeout_ms?: number }).timeout_ms === "number"
          ? (params as { timeout_ms: number }).timeout_ms
          : 120_000;
        const result = await runMeridianCommand(["spawn", "wait", spawnId], timeoutMs);
        const status = parseStatus(result.stdout) ?? "unknown";
        bus.emit("meridian:spawn:updated", { spawn_id: spawnId, status });
        return {
          content: [{ type: "text", text: result.stdout || result.stderr }],
          details: { action: "wait", success: result.exitCode === 0, spawn_id: spawnId, status },
          isError: result.exitCode !== 0,
        };
      }

      if (action === "clear") {
        await manager.tree.write({ nodes: [], updated_at_ms: Date.now() });
        return {
          content: [{ type: "text", text: "Cleared spawn tree projection." }],
          details: { action: "clear", success: true },
        };
      }

      return {
        content: [{ type: "text", text: `Unknown action '${String(action)}'` }],
        details: { success: false },
        isError: true,
      };
    },
  });
}
