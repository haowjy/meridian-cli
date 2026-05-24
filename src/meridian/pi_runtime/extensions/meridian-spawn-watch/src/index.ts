import type { ExtensionAPI } from "../../types";
import { resolveExtensionBus } from "../../shared/meridian_event_bus";
import { isSpawnWatchExtensionEnabled } from "../../shared/pi_harness_profile";
import { startSpawnCollector } from "./collector";
import { setupLifecycleSession } from "./lifecycle_session";
import { setupSpawnsCommands } from "./commands";
import {
  resolveSessionId,
  resolveStateRoot,
  SpawnWatchManager,
} from "./spawn_manager";
import { setupSpawnWatchTool } from "./tools";

type PiExtension = ExtensionAPI & {
  on?: (event: string, handler: (...args: unknown[]) => unknown) => void;
};

export default function meridianSpawnWatchExtension(pi: ExtensionAPI): void {
  if (!isSpawnWatchExtensionEnabled()) {
    return;
  }
  const bus = resolveExtensionBus(pi);
  const sessionId = resolveSessionId({ sessionManager: pi.session });
  const manager = new SpawnWatchManager({
    stateRoot: resolveStateRoot(),
    sessionId,
  });

  setupLifecycleSession(pi);
  const stopCollector = startSpawnCollector(manager.tree, bus);
  setupSpawnWatchTool(pi, manager, bus);
  setupSpawnsCommands(pi as PiExtension, manager, bus);

  pi.on?.("session_start", async (_event, ctx) => {
    manager.sessionId = resolveSessionId(ctx);
  });

  pi.on?.("session_shutdown", async () => {
    stopCollector();
  });
}
