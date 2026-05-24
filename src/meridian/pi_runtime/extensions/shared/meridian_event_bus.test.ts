import { describe, expect, it, vi } from "vitest";

import { createLocalBus, resolveExtensionBus } from "./meridian_event_bus";
import type { ExtensionAPI } from "../types";

describe("resolveExtensionBus", () => {
  it("uses pi.events when it implements emit/on", () => {
    const payloads: Record<string, unknown>[] = [];
    const pi = {
      events: {
        emit: (_channel: string, payload: Record<string, unknown>) => {
          payloads.push(payload);
        },
        on: () => () => undefined,
      },
    } as unknown as ExtensionAPI;

    const bus = resolveExtensionBus(pi);
    bus.emit("meridian:test", { ok: true });
    expect(payloads).toEqual([{ ok: true }]);
  });

  it("falls back to a local bus when pi.events is missing", () => {
    const pi = {} as ExtensionAPI;
    const bus = resolveExtensionBus(pi);
    const handler = vi.fn();
    const unsub = bus.on("meridian:test", handler);
    bus.emit("meridian:test", { value: 1 });
    expect(handler).toHaveBeenCalledWith({ value: 1 });
    unsub();
    bus.emit("meridian:test", { value: 2 });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(createLocalBus).toBeDefined();
  });
});
