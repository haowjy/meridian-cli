import { afterEach, describe, expect, it } from "vitest";

import { resolvePiHarnessProfile } from "./pi_harness_profile.js";

const PUBLIC_PREFIX = "MERIDIAN_PI_";
const BACKGROUND = "BACKGROUND_TASKS_ENABLED";
const SPAWN_WATCH = "SPAWN_WATCH_ENABLED";
const INTERNAL_PREFIX = "_MERIDIAN_PI_";

const touched = [
  PUBLIC_PREFIX + BACKGROUND,
  PUBLIC_PREFIX + SPAWN_WATCH,
  INTERNAL_PREFIX + BACKGROUND,
  INTERNAL_PREFIX + SPAWN_WATCH,
];

afterEach(() => {
  for (const name of touched) {
    delete process.env[name];
  }
});

describe("Pi harness profile transport", () => {
  it("reads only the repo-internal bundle toggle names", () => {
    process.env[PUBLIC_PREFIX + BACKGROUND] = "0";
    process.env[PUBLIC_PREFIX + SPAWN_WATCH] = "0";

    expect(resolvePiHarnessProfile()).toMatchObject({
      background_tasks_enabled: true,
      spawn_watch_enabled: true,
    });

    process.env[INTERNAL_PREFIX + BACKGROUND] = "0";
    process.env[INTERNAL_PREFIX + SPAWN_WATCH] = "0";

    expect(resolvePiHarnessProfile()).toMatchObject({
      background_tasks_enabled: false,
      spawn_watch_enabled: false,
    });
  });
});
