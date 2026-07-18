import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { readSpawnOriginBashIds, rememberSpawnOriginBashIds } from "./spawn_origins";

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

describe("spawn origin ids", () => {
  afterEach(() => restoreEnv());

  it("serializes concurrent origin writes", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "spawn-origins-"));
    setEnv("_MERIDIAN_PI_STATE_DIR", runtimeRoot);

    try {
      await Promise.all([
        rememberSpawnOriginBashIds(["b-one"], "p-parent"),
        rememberSpawnOriginBashIds(["b-two"], "p-parent"),
      ]);

      expect([...(await readSpawnOriginBashIds("p-parent"))].sort()).toEqual(["b-one", "b-two"]);
    } finally {
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });
});
