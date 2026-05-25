import { describe, expect, it, vi } from "vitest";

import { backgroundForegroundBash } from "../background_foreground";
import { USER_BASH_PANEL_BACKGROUND_MSG } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";
import { registerPsBackgroundCommands } from "./background";

vi.mock("../background_foreground", () => ({
  backgroundForegroundBash: vi.fn(),
}));

describe("registerPsBackgroundCommands", () => {
  it("registers ps:b and ps:background with the same behavior", async () => {
    vi.mocked(backgroundForegroundBash).mockResolvedValue({ ok: true });

    const handlers = new Map<string, (args: string, ctx: unknown) => Promise<void>>();
    const panelHost = {} as TaskPanelHost;
    const pi = {
      registerCommand: (
        name: string,
        spec: { handler: (args: string, ctx: unknown) => Promise<void> },
      ) => {
        handlers.set(name, spec.handler);
      },
    };

    registerPsBackgroundCommands(pi as never, panelHost);

    expect(handlers.has("ps:b")).toBe(true);
    expect(handlers.has("ps:background")).toBe(true);

    const notify = vi.fn();
    const custom = vi.fn();
    const ctx = { ui: { notify, custom }, hasUI: true };

    await handlers.get("ps:b")!("", ctx);
    expect(backgroundForegroundBash).toHaveBeenCalledWith(panelHost);
    expect(notify).toHaveBeenCalledWith(USER_BASH_PANEL_BACKGROUND_MSG, "info");

    vi.mocked(backgroundForegroundBash).mockClear();
    notify.mockClear();

    await handlers.get("ps:background")!("", ctx);
    expect(backgroundForegroundBash).toHaveBeenCalledWith(panelHost);
    expect(notify).toHaveBeenCalledWith(USER_BASH_PANEL_BACKGROUND_MSG, "info");
    expect(custom).not.toHaveBeenCalled();
  });

  it("notifies when no foreground task", async () => {
    vi.mocked(backgroundForegroundBash).mockResolvedValue({ ok: false, reason: "no_foreground" });

    let handler: ((args: string, ctx: unknown) => Promise<void>) | undefined;
    const pi = {
      registerCommand: (
        name: string,
        spec: { handler: (args: string, ctx: unknown) => Promise<void> },
      ) => {
        if (name === "ps:b") {
          handler = spec.handler;
        }
      },
    };

    registerPsBackgroundCommands(pi as never, {} as TaskPanelHost);

    const notify = vi.fn();
    await handler!("", { ui: { notify }, hasUI: true });

    expect(notify).toHaveBeenCalledWith("No foreground $ task to background", "warning");
  });
});
