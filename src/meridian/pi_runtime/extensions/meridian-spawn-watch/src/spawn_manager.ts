import path from "node:path";

import { resolveStateRoot } from "../../shared/pi_state_paths";
import { SpawnTreeStore } from "./tree";

export { resolveStateRoot };

export type SpawnWatchManagerOptions = {
  stateRoot: string;
  sessionId: string;
};

export class SpawnWatchManager {
  readonly tree: SpawnTreeStore;
  sessionId: string;

  constructor(options: SpawnWatchManagerOptions) {
    this.sessionId = options.sessionId;
    const treePath = path.join(
      options.stateRoot,
      "meridian-spawn-watch",
      options.sessionId,
      "tree.json",
    );
    this.tree = new SpawnTreeStore(treePath);
  }
}

export function resolveSessionId(context: unknown): string {
  const ctx = context as {
    sessionManager?: { getSessionId?: () => string };
  };
  return ctx.sessionManager?.getSessionId?.() ?? "default";
}
