import { describe, expect, it } from "vitest";

import { parseSpawnStateFile } from "./schemas";

describe("spawn state schema", () => {
  const base = {
    id: "p1", parent_id: "p-parent", status: "failed",
    terminal: { exit_code: 1, finished_at: "2026-01-01T00:00:00Z", published_at: "2026-01-01T00:00:00Z" },
  };

  it("accepts missing or null terminal errors and rejects a malformed present error", () => {
    expect(parseSpawnStateFile(base)).not.toBeNull();
    expect(parseSpawnStateFile({ ...base, terminal: { ...base.terminal, error: null } })).not.toBeNull();
    expect(parseSpawnStateFile({ ...base, terminal: { ...base.terminal, error: 7 } })).toBeNull();
  });
});
