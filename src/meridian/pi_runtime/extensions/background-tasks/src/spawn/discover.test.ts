import { afterEach, describe, expect, it, vi } from "vitest";

const { fetchSpawnChildrenIds, readFileSync } = vi.hoisted(() => ({
  fetchSpawnChildrenIds: vi.fn(),
  readFileSync: vi.fn(),
}));

vi.mock("./spawn_record", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./spawn_record")>();
  return {
    ...actual,
    fetchSpawnChildrenIds,
  };
});

vi.mock("node:fs", () => ({
  readFileSync,
}));

import { collectSpawnCandidateIds, collectSpawnCandidates } from "./discover";

describe("collectSpawnCandidates", () => {
  afterEach(() => {
    fetchSpawnChildrenIds.mockReset();
    readFileSync.mockReset();
  });

  it("scopes CLI discovery to spawn children, not global spawn list", async () => {
    fetchSpawnChildrenIds.mockResolvedValue(["p100"]);
    readFileSync.mockReturnValue("Spawn id: p200\n");

    const registry = {
      list: vi.fn().mockResolvedValue([
        {
          task_id: "t-launch",
          command: "uv run meridian spawn -m x",
          combined_log_path: "/tmp/task.log",
        },
      ]),
    };

    const candidates = await collectSpawnCandidates(registry as never, "p42");

    expect(fetchSpawnChildrenIds).toHaveBeenCalledWith("p42");
    expect(candidates.get("p100")).toEqual({});
    expect(candidates.get("p200")).toEqual({ task_id: "t-launch" });
  });

  it("does not attach spawn ids from wait task logs", async () => {
    readFileSync.mockReturnValue("Spawn id: p99\np100 p101\n");

    const registry = {
      list: vi.fn().mockResolvedValue([
        {
          task_id: "t-wait",
          command: "uv run meridian spawn wait",
          combined_log_path: "/tmp/wait.log",
        },
      ]),
    };

    const candidates = await collectSpawnCandidates(registry as never, null);
    expect(candidates.size).toBe(0);
  });

  it("skips children CLI when host spawn id is unset", async () => {
    const candidates = await collectSpawnCandidates(null, null);
    expect(fetchSpawnChildrenIds).not.toHaveBeenCalled();
    expect(candidates.size).toBe(0);
  });
});

describe("collectSpawnCandidateIds", () => {
  it("returns id keys from collectSpawnCandidates", async () => {
    fetchSpawnChildrenIds.mockResolvedValue([]);
    const registry = { list: vi.fn().mockResolvedValue([]) };
    const ids = await collectSpawnCandidateIds(registry as never, null);
    expect(ids).toEqual(new Set());
  });
});
