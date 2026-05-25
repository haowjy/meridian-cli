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

import { collectSpawnCandidateIds } from "./discover";

describe("collectSpawnCandidateIds", () => {
  afterEach(() => {
    fetchSpawnChildrenIds.mockReset();
    readFileSync.mockReset();
  });

  it("scopes CLI discovery to spawn children, not global spawn list", async () => {
    fetchSpawnChildrenIds.mockResolvedValue(["p100"]);
    readFileSync.mockReturnValue("launched p200\n");

    const registry = {
      list: vi.fn().mockResolvedValue([{ combined_log_path: "/tmp/task.log" }]),
    };

    const ids = await collectSpawnCandidateIds(registry as never, "p42");

    expect(fetchSpawnChildrenIds).toHaveBeenCalledWith("p42");
    expect(fetchSpawnChildrenIds).toHaveBeenCalledTimes(1);
    expect(ids).toEqual(new Set(["p100", "p200"]));
  });

  it("skips children CLI when host spawn id is unset", async () => {
    const ids = await collectSpawnCandidateIds(null, null);
    expect(fetchSpawnChildrenIds).not.toHaveBeenCalled();
    expect(ids.size).toBe(0);
  });
});
