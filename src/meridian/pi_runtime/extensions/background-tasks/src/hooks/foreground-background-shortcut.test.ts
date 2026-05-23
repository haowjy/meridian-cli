import { describe, expect, it, vi } from "vitest";

import { backgroundForegroundBash } from "../background_foreground";
import { setForegroundUserBashTaskId } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";
import { stepCtrlBBackground } from "./foreground-background-shortcut";

const CTRL_B = "\x02";

describe("stepCtrlBBackground", () => {
  it("ignores non-ctrl+b input", () => {
    expect(stepCtrlBBackground("q", true)).toEqual({
      action: "ignore",
      consume: false,
    });
  });

  it("does not consume when no foreground bash", () => {
    expect(stepCtrlBBackground(CTRL_B, false)).toEqual({
      action: "ignore",
      consume: false,
    });
  });

  it("backgrounds on single ctrl+b with foreground", () => {
    expect(stepCtrlBBackground(CTRL_B, true)).toEqual({
      action: "background",
      consume: true,
    });
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
