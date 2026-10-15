import { afterEach, expect, test, vi } from "vitest";
import {
  currentRunIdFromEnv,
  resolveBashRecordsPath,
  resolveClearedSpawnsPath,
  resolveDeliveryFaultPath,
  resolveDeliveryReceiptsPath,
  resolveObservedSpawnsPath,
  resolveRunScopeDir,
  resolveSpawnScopeDir,
} from "./pi_state_paths";

afterEach(() => vi.unstubAllEnvs());

test("old launchers keep their exact paths; new runs separate receipts from child ledger", () => {
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", "/tmp/identity-fixture");
  vi.stubEnv("MERIDIAN_SPAWN_ID", "p2");
  vi.stubEnv("MERIDIAN_RUN_ID", "");
  expect(currentRunIdFromEnv()).toBe("p2");
  expect(resolveBashRecordsPath()).toBe("/tmp/identity-fixture/pi-bash/p2/bash-records.json");
  vi.stubEnv("MERIDIAN_RUN_ID", "p2.3");
  expect(resolveSpawnScopeDir()).toBe("/tmp/identity-fixture/pi-bash/p2");
  expect(resolveRunScopeDir()).toBe("/tmp/identity-fixture/pi-bash/p2.3");
  expect(resolveBashRecordsPath()).toBe("/tmp/identity-fixture/pi-bash/p2.3/bash-records.json");
  expect(resolveDeliveryReceiptsPath()).toBe("/tmp/identity-fixture/pi-bash/p2.3/delivery-receipts.json");
  expect(resolveDeliveryFaultPath()).toBe("/tmp/identity-fixture/pi-bash/p2.3/delivery-fault.json");
  expect(resolveObservedSpawnsPath()).toBe("/tmp/identity-fixture/pi-bash/p2/observed-spawns.json");
  expect(resolveClearedSpawnsPath()).toBe("/tmp/identity-fixture/pi-bash/p2/cleared-spawns.json");
});
