import { afterEach, expect, it, vi } from "vitest";

// `ps` answers are driven by the test so probes can be ordered deterministically.
const psQueries = vi.hoisted(() => [] as Array<(error: Error | null, result?: { stdout: string; stderr: string }) => void>);
vi.mock("node:child_process", async (importOriginal) => ({
  ...(await importOriginal<typeof import("node:child_process")>()),
  execFile: (_command: string, _args: string[], _options: object, callback: (typeof psQueries)[number]) => { psQueries.push(callback); },
}));

import { ShellTask } from "./shell_task";

afterEach(() => { vi.restoreAllMocks(); psQueries.length = 0; });

/** Make every group probe see a live group (as a reused group id would) without signalling anything. */
function groupsLookLive() {
  const real = process.kill.bind(process);
  return vi.spyOn(process, "kill").mockImplementation((pid, signal) => pid < 0 ? true : real(pid, signal));
}

it("retires group-signalling authority once the group is observed gone", async () => {
  const task = new ShellTask("true", process.cwd(), process.env);
  await task.waitForExit();

  const kill = groupsLookLive();
  await task.terminate();
  expect(kill.mock.calls.filter(([pid]) => pid < 0)).toEqual([]);
});

it("treats a retirement observed by another probe as final for an outstanding ps query", async () => {
  const task = new ShellTask("true", process.cwd(), process.env);
  await task.closed;
  await vi.waitFor(() => expect(task.child.exitCode).not.toBeNull());
  groupsLookLive();

  const exiting = task.waitForExit();
  await vi.waitFor(() => expect(psQueries).toHaveLength(1));
  const terminating = task.terminate();
  await vi.waitFor(() => expect(psQueries).toHaveLength(2));
  psQueries[1]!(null, { stdout: "", stderr: "" }); // a second probe finds nobody and retires the group
  await terminating;
  psQueries[0]!(new Error("ps timed out")); // the stale query now fails
  await expect(exiting).resolves.toBe(0);
});

it("retires on ESRCH from a signal, without probing a potentially reused group afterwards", async () => {
  const task = new ShellTask("true", process.cwd(), process.env);
  await task.closed;
  const kill = vi.spyOn(process, "kill").mockImplementation((_pid, signal) => {
    if (signal === "SIGTERM") throw Object.assign(new Error("gone"), { code: "ESRCH" });
    return true; // a subsequent probe would see an unrelated reused group
  });
  const terminating = task.terminate();
  // Release any erroneous probe too, so the negative control cannot hang its fixture.
  for (const query of psQueries.splice(0)) query(null, { stdout: "", stderr: "" });
  await terminating;
  expect(kill.mock.calls).toEqual([[-task.child.pid!, "SIGTERM"]]);
});
