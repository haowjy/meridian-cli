import { describe, expect, it } from "vitest";

import { createLocalBus } from "../../shared/meridian_event_bus";
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
  it("attaches spawn to launcher task row instead of duplicating", () => {
    const bus = createLocalBus();
    const feed = createUnifiedRowFeed(bus);
    bus.emit("meridian:spawn:discovered", {
      spawn_id: "pabc",
      status: "running",
      task_id: "t2",
    });
    const rows = feed.mergeRows([
      task({ task_id: "t1", command: "sleep 1", label: "sleep" }),
      task({
        task_id: "t2",
        command: "uv run meridian spawn -m x",
        label: "spawn",
      }),
    ]);
    expect(rows).toHaveLength(2);
    const launcher = rows.find((r) => r.kind === "process" && r.task_id === "t2");
    expect(launcher?.meridian_spawn?.spawn_id).toBe("pabc");
    expect(rows.some((r) => r.kind === "meridian_spawn")).toBe(false);
    feed.dispose();
  });

  it("keeps orphan spawn row when no task_id attachment", () => {
    const bus = createLocalBus();
    const feed = createUnifiedRowFeed(bus);
    bus.emit("meridian:spawn:discovered", {
      spawn_id: "p1",
      status: "running",
    });
    const rows = feed.mergeRows([]);
    expect(rows).toHaveLength(1);
    expect(rows[0]?.kind).toBe("meridian_spawn");
    feed.dispose();
  });

  it("removes spawn rows on meridian:spawn:removed", () => {
    const bus = createLocalBus();
    const feed = createUnifiedRowFeed(bus);
    bus.emit("meridian:spawn:discovered", { spawn_id: "p1", status: "running" });
    bus.emit("meridian:spawn:removed", { spawn_id: "p1" });
    const rows = feed.mergeRows([]);
    expect(rows.some((r) => r.kind === "meridian_spawn")).toBe(false);
    feed.dispose();
  });
});
