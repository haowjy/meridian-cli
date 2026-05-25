import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { runMeridianCommand } from "../../../shared/meridian_cli";
import { isMeridianSpawnId } from "../../../shared/meridian_spawn";
import { parseSpawnStatus } from "../spawn/spawn_record";

/** Align with CLI checkpoint wait (30m subprocess cap; CLI may checkpoint earlier). */
const MSPAWN_WAIT_TIMEOUT_MS = 30 * 60 * 1000;

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

export function setupMspawnCommands(pi: PiWithCommands): void {
  if (!pi.registerCommand) {
    return;
  }

  pi.registerCommand("mspawn:wait", {
    description: "Wait for spawn completion via meridian spawn wait",
    handler: async (args, ctx) => {
      const spawnId = args.trim().split(/\s+/)[0] ?? "";
      if (!spawnId) {
        notify(ctx, "mspawn:wait requires spawn_id (e.g. p1234)", "warning");
        return;
      }
      if (!isMeridianSpawnId(spawnId)) {
        notify(ctx, `mspawn:wait requires a spawn id like p1234 (got ${spawnId})`, "warning");
        return;
      }
      const result = await runMeridianCommand(
        ["spawn", "wait", spawnId],
        MSPAWN_WAIT_TIMEOUT_MS,
      );
      const status = parseSpawnStatus(result.stdout) ?? "unknown";
      const text =
        (result.stdout || result.stderr).trim() ||
        `wait ${spawnId}: exit=${result.exitCode ?? "?"} status=${status}`;
      const level = (result.exitCode ?? 1) === 0 ? "info" : "warning";
      notify(ctx, text, level);
    },
  });
}
