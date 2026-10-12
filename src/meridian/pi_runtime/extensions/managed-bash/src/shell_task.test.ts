import { afterEach, expect, it, vi } from "vitest";

import { ShellTask } from "./shell_task";

afterEach(() => vi.restoreAllMocks());

it("retires group-signalling authority once the group is observed gone", async () => {
  const task = new ShellTask("true", process.cwd(), process.env);
  await task.waitForExit();

  // Model a reused group id: the probe would now report a live group, and signals would land.
  const kill = vi.spyOn(process, "kill").mockImplementation(() => true);
  await task.terminate();
  expect(kill.mock.calls.filter(([pid]) => pid < 0)).toEqual([]);
});
