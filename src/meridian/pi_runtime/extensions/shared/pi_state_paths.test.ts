import { afterEach, expect, test, vi } from "vitest";
import { currentRunIdFromEnv, resolveBashRecordsPath, resolveDeliveryReceiptsPath, resolveObservedSpawnsPath, resolveClearedSpawnsPath } from "./pi_state_paths";

afterEach(() => vi.unstubAllEnvs());

test("old launchers keep their exact paths; new runs separate receipts from child ledger", () => {
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", "/tmp/identity-fixture");
  vi.stubEnv("MERIDIAN_SPAWN_ID", "p2");
  vi.stubEnv("MERIDIAN_RUN_ID", "");
  expect(currentRunIdFromEnv()).toBe("p2");
  expect(resolveBashRecordsPath()).toBe("/tmp/identity-fixture/pi-bash/p2/bash-records.json");
  vi.stubEnv("MERIDIAN_RUN_ID", "p2.3");
  expect(resolveBashRecordsPath()).toBe("/tmp/identity-fixture/pi-bash/p2.3/bash-records.json");
  expect(resolveDeliveryReceiptsPath()).toContain("/p2.3/");
  expect(resolveObservedSpawnsPath()).toContain("/p2/");
  expect(resolveClearedSpawnsPath()).toContain("/p2/");
});
