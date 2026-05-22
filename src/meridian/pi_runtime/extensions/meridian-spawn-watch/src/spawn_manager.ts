import path from "node:path";

import { SpawnTreeStore } from "./tree";

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

export function resolveStateRoot(): string {
  const explicit = process.env.MERIDIAN_PI_STATE_DIR?.trim();
  if (explicit) {
    return explicit;
  }
  return path.join(process.env.HOME ?? "/tmp", ".meridian", "pi-state");
}

export function resolveSessionId(context: unknown): string {
  const ctx = context as {
    sessionManager?: { getSessionId?: () => string };
  };
  return ctx.sessionManager?.getSessionId?.() ?? "default";
}
