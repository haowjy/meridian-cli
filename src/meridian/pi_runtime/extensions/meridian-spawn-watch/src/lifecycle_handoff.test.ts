import { describe, expect, it, vi } from "vitest";

import { createLocalBus } from "../../shared/meridian_event_bus";
import { createLifecycleChildTracker } from "./lifecycle_child_tracker";
import { registerLifecycleToolHandlers } from "./lifecycle_tool_handlers";
import type { ExtensionAPI } from "../../types";

function makePi(bus = createLocalBus()): ExtensionAPI & {
  hooks: Map<string, Array<(...args: unknown[]) => unknown>>;
} {
  const hooks = new Map<string, Array<(...args: unknown[]) => unknown>>();
  return {
    hooks,
    events: bus,
    on(name: string, handler: (...args: unknown[]) => unknown) {
      const list = hooks.get(name) ?? [];
      list.push(handler);
      hooks.set(name, list);
      return () => hooks.set(name, (hooks.get(name) ?? []).filter((item) => item !== handler));
    },
    sendMessage: vi.fn(),
    session: { getSessionId: () => "sess-test" },
  } as unknown as ExtensionAPI & {
    hooks: Map<string, Array<(...args: unknown[]) => unknown>>;
  };
}

describe("lifecycle wrapper handoff", () => {
  it("fail-fast settles tracked wrapper when child spawn id is missing", async () => {
    const bus = createLocalBus();
    const pi = makePi(bus);
    const tracker = createLifecycleChildTracker(pi, bus);
    tracker.registerBusListeners();
    registerLifecycleToolHandlers(pi, tracker);

    const ends: Record<string, unknown>[] = [];
    bus.on("meridian:subspawn:end", (payload) => {
      ends.push(payload);
    });

    bus.emit("meridian:subspawn:start", {
      subspawn_id: "wrapper-job",
      wait_policy: "tracked",
      kind: "meridian_spawn",
      command: "meridian spawn run echo hi",
      command_is_meridian_spawn: true,
    });

    bus.emit("meridian:subspawn:end", {
      subspawn_id: "wrapper-job",
      wait_policy: "tracked",
      kind: "meridian_spawn",
      status: "exited",
      success: true,
      log_path: "",
    });

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(tracker.session.trackedChildren.has("wrapper-job")).toBe(false);
    expect(tracker.session.meridianSpawnWrapperJobs.has("wrapper-job")).toBe(false);
    const failureEnd = ends.find(
      (payload) =>
        payload.subspawn_id === "wrapper-job" &&
        payload.status === "failed" &&
        payload.reason === "wrapper_handoff_missing_child_id",
    );
    expect(failureEnd).toBeTruthy();
  });
});
