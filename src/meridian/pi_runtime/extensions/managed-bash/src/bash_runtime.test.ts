import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { parseBashRecordsFile } from "../../shared/schemas";
import { BashRuntime } from "./bash_runtime";

const savedEnv: Record<string, string | undefined> = {};

function setEnv(key: string, value: string): void {
  if (!(key in savedEnv)) savedEnv[key] = process.env[key];
  process.env[key] = value;
}

function restoreEnv(): void {
  for (const [key, value] of Object.entries(savedEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
    delete savedEnv[key];
  }
}

async function waitFor(predicate: () => boolean | Promise<boolean>, timeoutMs = 1000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("timed out waiting for condition");
}

describe("BashRuntime task pings", () => {
  afterEach(() => restoreEnv());

  it("keeps the shell running after ping failure and retries with rebound hooks", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "pi-bash-ping-failure-"));
    setEnv("_MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-test-ping-failure");
    setEnv("_MERIDIAN_PI_TASK_PING_INTERVAL_MS", "20");
    let failedAttempts = 0;
    const unhandled: unknown[] = [];
    const onUnhandled = (error: unknown): void => { unhandled.push(error); };
    process.on("unhandledRejection", onUnhandled);
    const runtime = new BashRuntime({ onBackgroundPing: () => {
      failedAttempts += 1;
      throw new Error("stale notification capability");
    } });
    try {
      const started = await runtime.execute({ command: "sleep 5", background: true }, undefined) as { bash_id: string };
      await waitFor(() => failedAttempts === 1);
      await new Promise((resolve) => setTimeout(resolve, 60));
      const row = runtime.list(true)[0]!;
      expect(row.status).toBe("running");
      expect(row.execution_error).toBeUndefined();
      expect(() => process.kill(-row.pid!, 0)).not.toThrow();
      expect(failedAttempts).toBe(1);
      const file = parseBashRecordsFile(JSON.parse(await readFile(path.join(runtimeRoot, "pi-bash", "p-test-ping-failure", "bash-records.json"), "utf8")));
      expect(file?.records[started.bash_id]?.ping_sent_at_ms).toBeNull();
      expect(file?.runtime_error).toBeUndefined();
      const pings: string[] = [];
      const rebound = new BashRuntime({ onBackgroundPing: (record) => { pings.push(record.bash_id); } });
      expect(rebound).toBe(runtime);
      await waitFor(() => pings.length === 1);
      await new Promise((resolve) => setTimeout(resolve, 60));
      expect(pings).toEqual([started.bash_id]);
      expect(unhandled).toEqual([]);
      expect(await rebound.manage({ action: "kill", bash_id: started.bash_id })).toMatchObject({ killed: true });
      expect(runtime.list(true)[0]?.status).toBe("killed");
    } finally {
      process.removeListener("unhandledRejection", onUnhandled);
      await runtime.shutdown();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("sends one ping for a tracked background command", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "pi-bash-ping-"));
    setEnv("_MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-test-ping");
    setEnv("_MERIDIAN_PI_BASH_ID", "b-12345678");
    setEnv("_MERIDIAN_PI_TASK_PING_INTERVAL_MS", "20");

    const pings: string[] = [];
    const runtime = new BashRuntime({
      onBackgroundPing: (record) => { pings.push(record.bash_id); },
    });

    try {
      const result = await runtime.execute(
        {
          command: `"${process.execPath}" -e "process.stdout.write(process.env._MERIDIAN_PI_BASH_ID);setTimeout(() => {}, 1000)"`,
          background: true,
        },
        undefined,
      );
      const bashId = (result as { bash_id: string }).bash_id;
      const file = parseBashRecordsFile(JSON.parse(await readFile(path.join(runtimeRoot, "pi-bash", "p-test-ping", "bash-records.json"), "utf8")));
      expect(file?.spawn_id).toBe("p-test-ping");
      expect(file?.records[bashId]?.originating_bash_id).toBe("b-12345678");
      await waitFor(() => runtime.list(true)[0]!.log_bytes > 0);
      expect(await runtime.manage({ action: "output", bash_id: bashId })).toMatchObject({ output: bashId });
      await waitFor(() => pings.length === 1);
      expect(pings).toEqual([bashId]);

      await new Promise((resolve) => setTimeout(resolve, 60));
      expect(pings).toEqual([bashId]);

      await runtime.manage({ action: "kill", bash_id: bashId });
    } finally {
      await runtime.shutdown();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });
});
