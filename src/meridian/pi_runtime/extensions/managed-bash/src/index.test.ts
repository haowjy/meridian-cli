import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import managedBashExtension from "./index";

const envKeys = [
  "_MERIDIAN_PI_STATE_DIR",
  "MERIDIAN_SPAWN_ID",
  "_MERIDIAN_PI_TASK_PING_INTERVAL_MS",
  "_MERIDIAN_PI_NOTIFICATION_GATE_VERSION",
  "_MERIDIAN_PI_NOTIFICATION_GATE_ATTEMPT",
  "_MERIDIAN_PI_NOTIFICATION_GATE_NONCE",
] as const;
const priorEnv = new Map<string, string | undefined>();

function setEnv(name: string, value: string | undefined): void {
  if (!priorEnv.has(name)) priorEnv.set(name, process.env[name]);
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

afterEach(() => {
  for (const [name, value] of priorEnv) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  priorEnv.clear();
});

async function waitFor(predicate: () => boolean, timeoutMs = 1_500): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate() && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 10));
  if (!predicate()) throw new Error("timed out waiting for background ping");
}

describe("managed-bash session selection", () => {
  it("preserves ordinary background pings when a later hook cancels selection", async () => {
    const stateDir = await mkdtemp(path.join(tmpdir(), "pi-bash-untracked-cancel-"));
    setEnv("_MERIDIAN_PI_STATE_DIR", stateDir);
    setEnv("MERIDIAN_SPAWN_ID", "p-untracked-cancel-test");
    setEnv("_MERIDIAN_PI_TASK_PING_INTERVAL_MS", "100");
    for (const name of envKeys.slice(3)) setEnv(name, undefined);

    const handlers = new Map<string, Array<(event?: unknown) => unknown>>();
    const tools = new Map<string, { execute: (...args: any[]) => Promise<unknown> }>();
    const sent: unknown[] = [];
    const api = {
      on: (name: string, handler: (event?: unknown) => unknown) => handlers.set(name, [...(handlers.get(name) ?? []), handler]),
      registerTool: (tool: { name: string; execute: (...args: any[]) => Promise<unknown> }) => tools.set(tool.name, tool),
      registerCommand: () => undefined,
      sendMessage: async (message: unknown) => { sent.push(message); },
    };
    managedBashExtension(api as never);
    // Pi stops at the first cancelling before-switch handler; managed-bash has already run.
    handlers.set("session_before_switch", [...(handlers.get("session_before_switch") ?? []), () => ({ cancel: true })]);

    try {
      await tools.get("bash")!.execute("test", { command: "exec sleep 3", background: true }, undefined);
      const eventResult = await (async () => {
        let result: unknown;
        for (const handler of handlers.get("session_before_switch") ?? []) {
          result = await handler({ reason: "resume" });
          if ((result as { cancel?: boolean } | undefined)?.cancel) break;
        }
        return result;
      })();
      expect((eventResult as { cancel?: boolean } | undefined)?.cancel).toBe(true);
      await waitFor(() => sent.length === 1);

      const file = path.join(stateDir, "pi-bash", "p-untracked-cancel-test", "bash-records.json");
      const state = JSON.parse(await readFile(file, "utf8")) as { records: Record<string, { status: string; ping_sent_at_ms: number | null }> };
      const [record] = Object.values(state.records);
      expect(record.status).toBe("running");
      expect(record.ping_sent_at_ms).not.toBeNull();
    } finally {
      for (const handler of handlers.get("session_shutdown") ?? []) await handler({ reason: "quit" });
      await rm(stateDir, { recursive: true, force: true });
    }
  });

});
