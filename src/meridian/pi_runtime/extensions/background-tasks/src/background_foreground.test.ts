import { describe, expect, it, vi } from "vitest";

import { backgroundForegroundBash } from "./background_foreground";
import { setForegroundUserBashTaskId } from "./bash_bridge";
import type { TaskPanelHost } from "./panel/host";

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
