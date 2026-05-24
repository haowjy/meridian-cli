import type { ExtensionAPI } from "../../types";
import { resolveExtensionBus } from "../../shared/meridian_event_bus";
import { createLifecycleChildTracker } from "./lifecycle_child_tracker";
import { registerLifecycleToolHandlers } from "./lifecycle_tool_handlers";

/** Wire spawn-watch lifecycle orchestration (waves, quiescence, wrapper handoff). */
export function setupLifecycleSession(pi: ExtensionAPI): void {
  const bus = resolveExtensionBus(pi);
  const tracker = createLifecycleChildTracker(pi, bus);
  tracker.registerBusListeners();
  registerLifecycleToolHandlers(pi, tracker);
  tracker.registerPiHooks();
}
