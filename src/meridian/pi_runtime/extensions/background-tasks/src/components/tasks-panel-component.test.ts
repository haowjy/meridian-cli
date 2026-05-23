import type { Theme } from "@earendil-works/pi-coding-agent";
import { describe, expect, it, vi } from "vitest";

import { setForegroundUserBashTaskId } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";
import type { PanelEntry } from "../panel/types";
import { TasksPanelComponent } from "./tasks-panel-component";

const sampleEntry: PanelEntry = {
  id: "t1",
  rowKey: "task:t1",
  kind: "task",
  name: "test",
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

function mockTheme(): Theme {
  const pass = (s: string) => s;
  return {
    fg: (_role: string, s: string) => pass(s),
    bold: pass,
  } as unknown as Theme;
}

function mockHost(entries: PanelEntry[] = []): TaskPanelHost {
  return {
    list: vi.fn(async () => entries),
    onEvent: vi.fn(() => () => {}),
    setSyncEntries: vi.fn(),
    kill: vi.fn(async () => ({ ok: true })),
    clearFinished: vi.fn(async () => 0),
    backgroundTask: vi.fn(async () => ({ ok: true })),
  } as unknown as TaskPanelHost;
}

describe("TasksPanelComponent handleInput", () => {
  it("handles quit without ReferenceError (plain and Kitty q)", () => {
    const onClose = vi.fn();
    const panel = new TasksPanelComponent(
      { requestRender: vi.fn() },
      mockTheme(),
      onClose,
      mockHost(),
    );

    expect(() => panel.handleInput("q")).not.toThrow();
    expect(onClose).toHaveBeenCalledTimes(1);

    onClose.mockClear();
    expect(() => panel.handleInput("\x1b[113u")).not.toThrow();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("handles navigation and action keys without ReferenceError", async () => {
    const panel = new TasksPanelComponent(
      { requestRender: vi.fn() },
      mockTheme(),
      vi.fn(),
      mockHost([sampleEntry, { ...sampleEntry, id: "t2", rowKey: "task:t2" }]),
    );
    await vi.waitFor(() => {
      expect(panel).toBeDefined();
    });

    for (const key of ["j", "k", "J", "K", "x", "c", "b", "\r", "\x1b[A", "\x1b[B"]) {
      expect(() => panel.handleInput(key)).not.toThrow();
    }
  });

  it("backgrounds the selected foreground row on b", async () => {
    setForegroundUserBashTaskId("fg-1");
    const backgroundTask = vi.fn(async () => ({ ok: true }));
    const host = mockHost([
      { ...sampleEntry, id: "fg-1", isForeground: true },
      { ...sampleEntry, id: "t2", rowKey: "task:t2" },
    ]);
    (host as { backgroundTask: typeof backgroundTask }).backgroundTask = backgroundTask;

    const panel = new TasksPanelComponent(
      { requestRender: vi.fn() },
      mockTheme(),
      vi.fn(),
      host,
    );
    await vi.waitFor(() => {
      expect(host.list).toHaveBeenCalled();
    });

    panel.handleInput("b");
    expect(backgroundTask).toHaveBeenCalledWith("fg-1");
    setForegroundUserBashTaskId(null);
  });
});
