import { describe, expect, it, vi } from "vitest";

import { backgroundForegroundBash } from "../background_foreground";
import { setForegroundUserBashTaskId } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";
import {
  CTRL_B_CHORD_WINDOW_MS,
  stepCtrlBBackgroundChord,
} from "./foreground-background-shortcut";

const CTRL_B = "\x02";

describe("stepCtrlBBackgroundChord", () => {
  it("ignores non-ctrl+b input", () => {
    const step = stepCtrlBBackgroundChord("q", 1000, null, true);
    expect(step).toEqual({ action: "ignore", consume: false, nextLastCtrlBAtMs: null });
  });

  it("does not consume when no foreground bash", () => {
    const step = stepCtrlBBackgroundChord(CTRL_B, 1000, null, false);
    expect(step).toEqual({ action: "ignore", consume: false, nextLastCtrlBAtMs: null });
  });

  it("arms on first ctrl+b with foreground", () => {
    const step = stepCtrlBBackgroundChord(CTRL_B, 1000, null, true);
    expect(step).toEqual({ action: "arm", consume: true, nextLastCtrlBAtMs: 1000 });
  });

  it("backgrounds on second ctrl+b within the window", () => {
    const step = stepCtrlBBackgroundChord(
      CTRL_B,
      1000 + CTRL_B_CHORD_WINDOW_MS,
      1000,
      true,
    );
    expect(step).toEqual({
      action: "background",
      consume: true,
      nextLastCtrlBAtMs: null,
    });
  });

  it("re-arms after the chord window expires", () => {
    const step = stepCtrlBBackgroundChord(
      CTRL_B,
      1000 + CTRL_B_CHORD_WINDOW_MS + 1,
      1000,
      true,
    );
    expect(step).toEqual({ action: "arm", consume: true, nextLastCtrlBAtMs: 1000 + CTRL_B_CHORD_WINDOW_MS + 1 });
  });
});

describe("backgroundForegroundBash", () => {
  it("calls host.backgroundTask for the foreground id", async () => {
    setForegroundUserBashTaskId("task-abc");

    const backgroundTask = vi.fn(async () => ({ ok: true }));
    const host = { backgroundTask } as unknown as TaskPanelHost;

    const result = await backgroundForegroundBash(host);
    expect(result).toEqual({ ok: true });
    expect(backgroundTask).toHaveBeenCalledWith("task-abc");

    setForegroundUserBashTaskId(null);
  });

  it("no-ops when no foreground id is set", async () => {
    setForegroundUserBashTaskId(null);

    const backgroundTask = vi.fn();
    const host = { backgroundTask } as unknown as TaskPanelHost;

    const result = await backgroundForegroundBash(host);
    expect(result).toEqual({ ok: false, reason: "no_foreground" });
    expect(backgroundTask).not.toHaveBeenCalled();
  });
});
