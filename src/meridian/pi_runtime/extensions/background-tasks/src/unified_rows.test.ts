import { describe, expect, it } from "vitest";

import { emitMeridianEvent } from "../../shared/meridian_bus";
import { createUnifiedRowFeed } from "./unified_rows";
import type { BackgroundTaskRecord } from "./types";

function task(partial: Partial<BackgroundTaskRecord> & { task_id: string; command: string }): BackgroundTaskRecord {
  return {
    label: partial.label ?? "job",
    cwd: "/tmp",
    pid: null,
    wait_policy: "tracked",
    status: "running",
    success: null,
    exit_code: null,
    signal: null,
    started_at_ms: Date.now(),
    ended_at_ms: null,
    stdout_log_path: "",
    stderr_log_path: "",
    combined_log_path: "",
    log_bytes: 0,
    log_truncated: false,
    ...partial,
  };
}

describe("createUnifiedRowFeed", () => {
  it("merges spawn bus rows with task rows", () => {
    const feed = createUnifiedRowFeed();
    emitMeridianEvent("meridian:spawn:discovered", {
      spawn_id: "pabc",
      status: "running",
    });
    const rows = feed.mergeRows([
      task({ task_id: "t1", command: "sleep 1", label: "sleep" }),
      task({ task_id: "t2", command: "meridian spawn foo", label: "spawn" }),
    ]);
    expect(rows).toHaveLength(3);
    expect(rows.find((r) => r.kind === "meridian_spawn")?.spawn_id).toBe("pabc");
    expect(rows.find((r) => r.kind === "meridian_spawn_wrapper")?.task_id).toBe("t2");
    feed.dispose();
  });

  it("removes spawn rows on meridian:spawn:removed", () => {
    const feed = createUnifiedRowFeed();
    emitMeridianEvent("meridian:spawn:discovered", { spawn_id: "p1", status: "running" });
    emitMeridianEvent("meridian:spawn:removed", { spawn_id: "p1" });
    const rows = feed.mergeRows([]);
    expect(rows.some((r) => r.kind === "meridian_spawn")).toBe(false);
    feed.dispose();
  });
});
