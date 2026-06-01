import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { readSpawnOriginBashIds } from "../../shared/spawn_origins";
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

  it("sends one ping for a tracked background command", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "pi-bash-ping-"));
    setEnv("MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-test-ping");
    setEnv("MERIDIAN_PI_TASK_PING_INTERVAL_MS", "20");

    const pings: string[] = [];
    const runtime = new BashRuntime({
      onBackgroundPing: (record) => pings.push(record.bash_id),
    });

    try {
      const result = await runtime.execute(
        {
          command: `"${process.execPath}" -e "setTimeout(() => {}, 1000)"`,
          background: true,
        },
        undefined,
      );
      const bashId = (result as { bash_id: string }).bash_id;
      await waitFor(async () => (await readSpawnOriginBashIds("p-test-ping")).has(bashId));
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
