import type { Theme } from "@earendil-works/pi-coding-agent";
import { describe, expect, it, vi } from "vitest";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

import type { TaskPanelHost } from "./host";
import { computePsColumnLayout, renderPsPanel } from "./ps-view";
import type { PanelEntry } from "./types";

function fitCell(value: string, width: number): string {
  const truncated = truncateToWidth(value, Math.max(0, width));
  const pad = Math.max(0, width - visibleWidth(truncated));
  return truncated + " ".repeat(pad);
}

function mockTheme(): Theme {
  const pass = (s: string) => s;
  return { fg: (_role: string, s: string) => pass(s), bold: pass } as unknown as Theme;
}

const sampleEntry: PanelEntry = {
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
  pingIntervalMs: null,
  nextPingAtMs: null,
  lastActivityAtMs: null,
  isLive: true,
};

describe("computePsColumnLayout", () => {
  it("truncates header cells to column width on narrow terminals", () => {
    const columns = computePsColumnLayout(40, 8, 8, 0);
    expect(visibleWidth(fitCell("Command", columns.cmdWidth))).toBe(columns.cmdWidth);
    expect(visibleWidth(fitCell("Status", columns.statusWidth))).toBe(columns.statusWidth);
  });
});

describe("renderPsPanel", () => {
  it("renders distinct Command and Status header cells when width is tight", () => {
    const host = {
      getOutput: vi.fn(() => null),
      getFileSize: vi.fn(() => ({ stdout: 0, stderr: 0 })),
    } as unknown as TaskPanelHost;

    const lines = renderPsPanel(44, 24, mockTheme(), host, {
      entries: [sampleEntry],
      selectedIndex: 0,
      processScrollOffset: 0,
      logScrollOffset: 0,
      backgroundingForeground: false,
      logScroll: { above: 0, below: 0 },
    });

    const headerLine = lines.find(
      (line) => line.includes("Process") && line.includes("Stat"),
    );
    expect(headerLine).toBeDefined();
    expect(headerLine).not.toMatch(/CommandStatus/);
  });
});
