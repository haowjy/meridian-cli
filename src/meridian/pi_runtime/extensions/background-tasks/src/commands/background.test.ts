import { describe, expect, it, vi } from "vitest";

import { backgroundForegroundBash } from "../background_foreground";
import { USER_BASH_PANEL_BACKGROUND_MSG } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";
import { registerPsBackgroundCommand } from "./background";

vi.mock("../background_foreground", () => ({
  backgroundForegroundBash: vi.fn(),
}));

describe("registerPsBackgroundCommand /ps:background", () => {
  it("backgrounds foreground task without opening overlay", async () => {
    vi.mocked(backgroundForegroundBash).mockResolvedValue({ ok: true });

    let handler: ((args: string, ctx: unknown) => Promise<void>) | undefined;
    const panelHost = {} as TaskPanelHost;
    const pi = {
      registerCommand: (
        name: string,
        spec: { handler: (args: string, ctx: unknown) => Promise<void> },
      ) => {
        expect(name).toBe("ps:background");
        handler = spec.handler;
      },
    };

    registerPsBackgroundCommand(pi as never, panelHost);

    const notify = vi.fn();
    const custom = vi.fn();
    await handler!("", {
      ui: { notify, custom },
      hasUI: true,
    });

    expect(backgroundForegroundBash).toHaveBeenCalledWith(panelHost);
    expect(notify).toHaveBeenCalledWith(USER_BASH_PANEL_BACKGROUND_MSG, "info");
    expect(custom).not.toHaveBeenCalled();
  });

  it("notifies when no foreground task", async () => {
    vi.mocked(backgroundForegroundBash).mockResolvedValue({ ok: false, reason: "no_foreground" });

    let handler: ((args: string, ctx: unknown) => Promise<void>) | undefined;
    const pi = {
      registerCommand: (
        _name: string,
        spec: { handler: (args: string, ctx: unknown) => Promise<void> },
      ) => {
        handler = spec.handler;
      },
    };

    registerPsBackgroundCommand(pi as never, {} as TaskPanelHost);

    const notify = vi.fn();
    await handler!("", { ui: { notify }, hasUI: true });

    expect(notify).toHaveBeenCalledWith("No foreground $ task to background", "warning");
  });
});
