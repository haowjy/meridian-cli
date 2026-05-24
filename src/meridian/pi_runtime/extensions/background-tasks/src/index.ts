import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { createLifecycleSidecarWriter } from "../../shared/lifecycle_sidecar";
import { isBackgroundTasksExtensionEnabled } from "../../shared/pi_harness_profile";
import { resolveExtensionBus } from "../../shared/meridian_event_bus";
import { getForegroundUserBashTaskId, setupBashBridge } from "./bash_bridge";
import { setupPsCommands } from "./commands";
import { setupForegroundBackgroundShortcut } from "./hooks/foreground-background-shortcut";
import { setupForegroundBashHint } from "./hooks/foreground-bash-hint";
import { setupTaskWidget } from "./hooks/widget";
import { TaskPanelHost } from "./panel/host";
import { resolveSpawnTaskPingDefaults } from "./session_ping";
import {
  TaskRegistry,
  makeId,
  parentSpawnIdFromEnv,
  resolveStateRoot,
  sessionIdFromContext,
  type ToolContext,
} from "./task_registry";
import { setupBackgroundTaskTool } from "./tools";
import { createUnifiedRowFeed } from "./unified_rows";

type PiExtension = ExtensionAPI & {
  on?: (event: string, handler: (...args: unknown[]) => unknown) => void;
};

const state: {
  registry: TaskRegistry | null;
  sessionId: string;
  createRegistry: ((sessionId: string) => TaskRegistry) | null;
  sidecar: ReturnType<typeof createLifecycleSidecarWriter> | null;
  panelHost: TaskPanelHost | null;
} = {
  registry: null,
  sessionId: makeId("session"),
  createRegistry: null,
  sidecar: null,
  panelHost: null,
};

async function buildRegistry(
  pi: PiExtension,
  bus: ReturnType<typeof resolveExtensionBus>,
): Promise<{
  registry: TaskRegistry;
  sessionId: string;
  createRegistry: (sessionId: string) => TaskRegistry;
}> {
  const sessionId = makeId("session");
  const sidecar = createLifecycleSidecarWriter();
  const spawnPingDefaults = resolveSpawnTaskPingDefaults();
  const createRegistry = (sid: string): TaskRegistry =>
    new TaskRegistry(
      resolveStateRoot(),
      sid,
      parentSpawnIdFromEnv(),
      bus,
      sidecar,
      spawnPingDefaults,
    );
  state.sidecar = sidecar;
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
    state.sidecar?.close();
  });

  await registry.initialize();
  return { registry, sessionId, createRegistry };
}

export default async function backgroundTasksExtension(pi: ExtensionAPI): Promise<void> {
  if (!isBackgroundTasksExtensionEnabled(true)) {
    return;
  }
  const piExt = pi as PiExtension;
  const bus = resolveExtensionBus(pi);
  const setup = await buildRegistry(piExt, bus);
  state.registry = setup.registry;
  state.sessionId = setup.sessionId;
  state.createRegistry = setup.createRegistry;

  const rowFeed = createUnifiedRowFeed(bus);
  const spawnPingDefaults = resolveSpawnTaskPingDefaults();
  state.panelHost = new TaskPanelHost(
    () => state.registry,
    rowFeed.mergeRows,
    bus,
    spawnPingDefaults,
    getForegroundUserBashTaskId,
  );

  const { dockActions } = setupTaskWidget(pi, state.panelHost);
  setupForegroundBackgroundShortcut(pi, () => state.panelHost);
  setupForegroundBashHint(pi);

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
  setupBashBridge(piExt, state);
  setupPsCommands(pi, state.panelHost, dockActions, {
    getRegistry: () => state.registry,
    mergeRows: rowFeed.mergeRows,
  });

  piExt.on?.("session_shutdown", async () => {
    rowFeed.dispose();
    state.panelHost?.dispose();
    state.panelHost = null;
  });
}
