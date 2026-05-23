import { afterEach, describe, expect, it } from "vitest";

import {
  DEFAULT_TASK_PING_INTERVAL_MS,
  resolveEffectivePingIntervalMs,
  resolveSpawnTaskPingDefaults,
  TASK_PING_INTERVAL_MS_ENV,
  TASK_PING_RESET_ON_ACTIVITY_ENV,
} from "./session_ping";

describe("resolveEffectivePingIntervalMs", () => {
  it("uses task override, then spawn default, then 55m fallback", () => {
    expect(resolveEffectivePingIntervalMs(120_000, 90_000)).toBe(120_000);
    expect(resolveEffectivePingIntervalMs(null, 90_000)).toBe(90_000);
    expect(resolveEffectivePingIntervalMs(undefined, null)).toBe(DEFAULT_TASK_PING_INTERVAL_MS);
  });
});

describe("resolveSpawnTaskPingDefaults", () => {
  const originalEnv = { ...process.env };

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it("reads spawn env overrides", () => {
    process.env[TASK_PING_INTERVAL_MS_ENV] = "5400000";
    process.env[TASK_PING_RESET_ON_ACTIVITY_ENV] = "false";
    const defaults = resolveSpawnTaskPingDefaults();
    expect(defaults.pingIntervalMs).toBe(5_400_000);
    expect(defaults.pingResetOnActivity).toBe(false);
  });
});
