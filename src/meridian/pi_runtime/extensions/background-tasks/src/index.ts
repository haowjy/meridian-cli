import { Type } from "typebox";

import type { ExtensionAPI } from "../../types";
import { setupPsCommands } from "./commands";
import { setupBackgroundTaskHooks } from "./hooks";
import {
  DEFAULT_BG_READ_BYTES,
  DEFAULT_BG_WAIT_TIMEOUT_MS,
  MAX_BG_READ_BYTES,
  MAX_BG_WAIT_TIMEOUT_MS,
  TaskRegistry,
  clamp,
  makeId,
  normalizeWaitPolicy,
  parentSpawnIdFromEnv,
  resolveStateRoot,
  sessionIdFromContext,
  toInt,
  trimCombinedTails,
  type ToolContext,
} from "./task_registry";
import { setupBackgroundTaskTool } from "./tools";
import { createUnifiedRowFeed } from "./unified_rows";

type PiExtension = ExtensionAPI & {
  on?: (event: string, handler: (...args: unknown[]) => unknown) => void;
  events?: { emit: (channel: string, payload: Record<string, unknown>) => void };
};

const state: {
  registry: TaskRegistry | null;
  sessionId: string;
  createRegistry: ((sessionId: string) => TaskRegistry) | null;
} = {
  registry: null,
  sessionId: makeId("session"),
  createRegistry: null,
};

async function buildRegistry(pi: PiExtension): Promise<{
  registry: TaskRegistry;
  sessionId: string;
  createRegistry: (sessionId: string) => TaskRegistry;
}> {
  const sessionId = makeId("session");
  const createRegistry = (sid: string): TaskRegistry =>
    new TaskRegistry(resolveStateRoot(), sid, parentSpawnIdFromEnv(), (channel, payload) => {
      pi.events?.emit(channel, payload);
    });
  const registry = createRegistry(sessionId);

  pi.on?.("session_start", async (_event, ctx) => {
    const resolved = sessionIdFromContext(ctx as ToolContext, sessionId);
    const startRegistry = createRegistry(resolved);
    await startRegistry.initialize();
    await state.registry?.shutdownCleanup();
    state.registry = startRegistry;
    state.sessionId = resolved;
  });

  pi.on?.("session_shutdown", async () => {
    await state.registry?.shutdownCleanup();
  });

  await registry.initialize();
  return { registry, sessionId, createRegistry };
}

export default async function backgroundTasksExtension(pi: ExtensionAPI): Promise<void> {
  const piExt = pi as PiExtension;
  const setup = await buildRegistry(piExt);
  state.registry = setup.registry;
  state.sessionId = setup.sessionId;
  state.createRegistry = setup.createRegistry;

  const rowFeed = createUnifiedRowFeed();
  setupBackgroundTaskTool(pi, {
    getRegistry: () => state.registry,
    getSessionId: () => state.sessionId,
    setSession: (sessionId, registry) => {
      state.sessionId = sessionId;
      state.registry = registry;
    },
    createRegistry: (sessionId) => state.createRegistry!(sessionId),
    mergeRows: rowFeed.mergeRows,
  });
  setupBackgroundTaskHooks(piExt, state);
  setupPsCommands(piExt, {
    getRegistry: () => state.registry,
    mergeRows: rowFeed.mergeRows,
  });

  piExt.on?.("session_shutdown", async () => {
    rowFeed.dispose();
  });
}
