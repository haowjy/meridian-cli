import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { SpawnWatchRuntime } from "./index";

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

async function waitFor(predicate: () => boolean, timeoutMs = 1000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("timed out waiting for condition");
}

type SpawnWatchRuntimeInternals = SpawnWatchRuntime & {
  watchCandidateSpawnDir(spawnId: string): void;
  resolveCandidate(spawnId: string): Promise<void>;
  candidateWatchers: Map<string, { close(): void }>;
  childSpawnIds: Set<string>;
};

async function writeSpawnState(runtimeRoot: string, spawnId: string, parentId: string | null): Promise<void> {
  const spawnDir = path.join(runtimeRoot, "spawns", spawnId);
  await mkdir(spawnDir, { recursive: true });
  await writeFile(
    path.join(spawnDir, "state.json"),
    JSON.stringify({ id: spawnId, parent_id: parentId, status: "running" }),
  );
}

describe("SpawnWatchRuntime candidate watches", () => {
  afterEach(() => restoreEnv());

  it("keeps candidates without state, then closes non-child candidates", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "spawn-watch-candidate-"));
    setEnv("MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-parent");

    await mkdir(path.join(runtimeRoot, "spawns", "p-other"), { recursive: true });

    const runtime = new SpawnWatchRuntime({} as ConstructorParameters<typeof SpawnWatchRuntime>[0]);
    const internals = runtime as SpawnWatchRuntimeInternals;

    try {
      const resolve = vi.spyOn(internals, "resolveCandidate");
      internals.watchCandidateSpawnDir("p-other");
      await waitFor(() => internals.candidateWatchers.has("p-other"));
      const close = vi.spyOn(internals.candidateWatchers.get("p-other")!, "close");
      await resolve.mock.results[0]!.value;

      expect(close).not.toHaveBeenCalled();
      expect(internals.candidateWatchers.has("p-other")).toBe(true);

      await writeSpawnState(runtimeRoot, "p-other", null);
      await internals.resolveCandidate("p-other");

      expect(close).toHaveBeenCalledOnce();
      expect(internals.candidateWatchers.has("p-other")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("adopts child candidates and closes their candidate watcher", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "spawn-watch-child-candidate-"));
    setEnv("MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-parent");

    await mkdir(path.join(runtimeRoot, "spawns", "p-child"), { recursive: true });

    const runtime = new SpawnWatchRuntime({} as ConstructorParameters<typeof SpawnWatchRuntime>[0]);
    const internals = runtime as SpawnWatchRuntimeInternals;

    try {
      const resolve = vi.spyOn(internals, "resolveCandidate");
      internals.watchCandidateSpawnDir("p-child");
      await waitFor(() => internals.candidateWatchers.has("p-child"));
      const close = vi.spyOn(internals.candidateWatchers.get("p-child")!, "close");
      await resolve.mock.results[0]!.value;

      expect(close).not.toHaveBeenCalled();
      expect(internals.candidateWatchers.has("p-child")).toBe(true);

      await writeSpawnState(runtimeRoot, "p-child", "p-parent");
      await internals.resolveCandidate("p-child");

      expect(internals.childSpawnIds.has("p-child")).toBe(true);
      expect(close).toHaveBeenCalledOnce();
      expect(internals.candidateWatchers.has("p-child")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });
});
