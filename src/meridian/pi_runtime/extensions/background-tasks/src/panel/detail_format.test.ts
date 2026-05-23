import { describe, expect, it } from "vitest";

import { formatTaskDetailLines } from "./detail_format";
import type { PanelEntry } from "./types";

const base: PanelEntry = {
  id: "t1",
  rowKey: "task:t1",
  kind: "task",
  name: "sleep",
  command: "sleep 1",
  cwd: "/tmp",
  pid: 1,
  startTime: Date.now(),
  endTime: null,
  status: "running",
  exitCode: null,
  success: null,
  combinedLogPath: "",
  logBytes: 0,
  persistent: false,
  pingIntervalMs: 3_300_000,
  nextPingAtMs: Date.now() + 3_300_000,
  lastActivityAtMs: Date.now(),
  isLive: true,
};

describe("formatTaskDetailLines", () => {
  it("omits ping fields from default detail", () => {
    expect(formatTaskDetailLines(base)).toEqual([]);
  });

  it("shows foreground and persistent when set", () => {
    const lines = formatTaskDetailLines({
      ...base,
      persistent: true,
      isForeground: true,
    });
    expect(lines).toContain("persistent");
    expect(lines.some((line) => line.includes("Foreground"))).toBe(true);
    expect(lines.some((line) => line.includes("ping"))).toBe(false);
  });
});
