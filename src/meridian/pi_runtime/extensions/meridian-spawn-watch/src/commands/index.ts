import type { ExtensionAPI } from "../../types";
import type { MeridianEventBus } from "../../shared/meridian_event_bus";
import type { SpawnWatchManager } from "../spawn_manager";
import {
  cancelSpawn,
  formatSpawnTree,
  showSpawn,
  waitSpawn,
} from "./actions";

type CommandCtx = {
  hasUI?: boolean;
  ui?: { notify?: (msg: string, level?: string) => void };
};

type PiWithCommands = ExtensionAPI & {
  registerCommand?: (
    name: string,
    spec: {
      description: string;
      handler: (args: string, ctx: CommandCtx) => Promise<void>;
    },
  ) => void;
};

function notify(ctx: CommandCtx, message: string, level = "info"): void {
  const text = message.slice(0, 8000);
  if (ctx.hasUI !== false && ctx.ui?.notify) {
    ctx.ui.notify(text, level);
    return;
  }
  process.stdout.write(`${text}\n`);
}

export function setupSpawnsCommands(
  pi: PiWithCommands,
  manager: SpawnWatchManager,
  bus: MeridianEventBus,
): void {
  if (!pi.registerCommand) {
    return;
  }

  pi.registerCommand("spawns", {
    description: "Show Meridian spawn tree for this session",
    handler: async (args, ctx) => {
      const file = await manager.tree.read();
      if (args.trim() === "json") {
        notify(ctx, JSON.stringify(file, null, 2));
        return;
      }
      notify(ctx, await formatSpawnTree(file));
    },
  });

  const register = (
    name: string,
    description: string,
    handler: (args: string, ctx: CommandCtx) => Promise<void>,
  ): void => {
    pi.registerCommand?.(`spawns:${name}`, { description, handler });
  };

  register("show", "Show one spawn (meridian spawn show)", async (args, ctx) => {
    const spawnId = args.trim();
    if (!spawnId) {
      notify(ctx, "spawns:show requires spawn_id", "warning");
      return;
    }
    notify(ctx, await showSpawn(bus, manager, spawnId));
  });

  register("cancel", "Cancel a running spawn", async (args, ctx) => {
    const spawnId = args.trim();
    if (!spawnId) {
      notify(ctx, "spawns:cancel requires spawn_id", "warning");
      return;
    }
    notify(ctx, await cancelSpawn(bus, spawnId));
  });

  register("wait", "Wait for spawn completion", async (args, ctx) => {
    const parts = args.trim().split(/\s+/);
    const spawnId = parts[0] ?? "";
    if (!spawnId) {
      notify(ctx, "spawns:wait requires spawn_id [timeout_ms]", "warning");
      return;
    }
    const timeoutMs = parts[1] ? Number.parseInt(parts[1], 10) : 120_000;
    notify(ctx, await waitSpawn(bus, spawnId, Number.isFinite(timeoutMs) ? timeoutMs : 120_000));
  });

  register("clear", "Clear session spawn tree projection", async (_args, ctx) => {
    await manager.tree.write({ nodes: [], updated_at_ms: Date.now() });
    notify(ctx, "Cleared spawn tree projection.");
  });

  register("logs", "Hint for task logs linked to a spawn", async (args, ctx) => {
    const spawnId = args.trim();
    if (!spawnId) {
      notify(ctx, "spawns:logs requires spawn_id", "warning");
      return;
    }
    const file = await manager.tree.read();
    const node = file.nodes.find((n) => n.spawn_id === spawnId);
    if (node?.task_id) {
      notify(ctx, `Use /ps:logs ${node.task_id} for wrapper task logs.`);
      return;
    }
    notify(ctx, await showSpawn(bus, manager, spawnId));
  });
}
