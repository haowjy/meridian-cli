import { EventEmitter } from "node:events";

import type { ExtensionAPI } from "../types";

/** In-process bus for Meridian extension coordination (Pi host `pi.events` in production). */
export type MeridianEventBus = {
  emit(channel: string, payload: Record<string, unknown>): void;
  on(channel: string, handler: (payload: Record<string, unknown>) => void): () => void;
};

export function createLocalBus(): MeridianEventBus {
  const emitter = new EventEmitter();
  return {
    emit(channel, payload) {
      emitter.emit(channel, payload);
    },
    on(channel, handler) {
      const listener = (payload: Record<string, unknown>) => handler(payload);
      emitter.on(channel, listener);
      return () => {
        emitter.off(channel, listener);
      };
    },
  };
}

function isMeridianEventBus(value: unknown): value is MeridianEventBus {
  if (!value || typeof value !== "object") {
    return false;
  }
  const bus = value as MeridianEventBus;
  return typeof bus.emit === "function" && typeof bus.on === "function";
}

/** Prefer Pi's shared extension bus; fall back to a local bus for tests and harness stubs. */
export function resolveExtensionBus(pi: ExtensionAPI): MeridianEventBus {
  if (isMeridianEventBus(pi.events)) {
    return pi.events;
  }
  return createLocalBus();
}
