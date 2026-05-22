import type { ExtensionAPI } from "../../types";
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
  const sessionId = resolveSessionId({ sessionManager: pi.session });
  const manager = new SpawnWatchManager({
    stateRoot: resolveStateRoot(),
    sessionId,
  });

  setupLifecycleSession(pi);
  startSpawnCollector(manager.tree);
  setupSpawnWatchTool(pi, manager);
  setupSpawnsCommands(pi as PiExtension, manager);

  pi.on?.("session_start", async (_event, ctx) => {
    manager.sessionId = resolveSessionId(ctx);
  });
}
