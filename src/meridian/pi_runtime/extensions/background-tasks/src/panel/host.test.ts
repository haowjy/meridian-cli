import { describe, expect, it, vi } from "vitest";

import { pinForegroundEntry, TaskPanelHost } from "./host";
import type { PanelEntry } from "./types";

function entry(id: string, startTime: number): PanelEntry {
  return {
    id,
    rowKey: `task:${id}`,
    kind: "task",
    name: id,
    command: "sleep",
    cwd: "/tmp",
    pid: 1,
    startTime,
    endTime: null,
    status: "running",
    exitCode: null,
    success: null,
    combinedLogPath: "",
    logBytes: 0,
    persistent: false,
    pingIntervalMs: null,
    nextPingAtMs: null,
    lastActivityAtMs: null,
    isLive: true,
  };
}

describe("pinForegroundEntry", () => {
  it("moves the foreground task to index 0 and marks it", () => {
    const sorted = [entry("t-new", 200), entry("t-old", 100)];
    const pinned = pinForegroundEntry(sorted, "t-old");
    expect(pinned[0]?.id).toBe("t-old");
    expect(pinned[0]?.isForeground).toBe(true);
    expect(pinned[1]?.id).toBe("t-new");
  });
});

describe("TaskPanelHost.backgroundTask", () => {
  it("releases wait only for the foreground id", async () => {
    const releaseWait = vi.fn(() => true);
    const host = new TaskPanelHost(
      () => ({ releaseWait }) as never,
      () => [],
      { on: () => () => {} },
      { pingIntervalMs: null, defaultPersistent: false },
      () => "fg-1",
    );

    await expect(host.backgroundTask("other")).resolves.toEqual({
      ok: false,
      reason: "not_foreground",
    });
    await expect(host.backgroundTask("fg-1")).resolves.toEqual({ ok: true });
    expect(releaseWait).toHaveBeenCalledWith("fg-1");
    host.dispose();
  });
});
