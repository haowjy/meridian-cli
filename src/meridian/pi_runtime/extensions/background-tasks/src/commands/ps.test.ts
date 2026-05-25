import { describe, expect, it, vi } from "vitest";

import { backgroundForegroundBash } from "../background_foreground";
import { USER_BASH_PANEL_BACKGROUND_MSG } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";
import {
  isPsBackgroundSubcommand,
  psBackgroundSubcommandMessage,
  registerPsCommand,
} from "./ps";

vi.mock("../background_foreground", () => ({
  backgroundForegroundBash: vi.fn(),
}));

describe("isPsBackgroundSubcommand", () => {
  it("matches b and background", () => {
    expect(isPsBackgroundSubcommand("b")).toBe(true);
    expect(isPsBackgroundSubcommand("background")).toBe(true);
    expect(isPsBackgroundSubcommand("json")).toBe(false);
    expect(isPsBackgroundSubcommand("")).toBe(false);
  });
});

describe("psBackgroundSubcommandMessage", () => {
  it("returns success message when ok", () => {
    expect(psBackgroundSubcommandMessage({ ok: true })).toEqual({
      level: "info",
      message: USER_BASH_PANEL_BACKGROUND_MSG,
    });
  });

  it("returns warning when no foreground", () => {
    expect(psBackgroundSubcommandMessage({ ok: false, reason: "no_foreground" })).toEqual({
      level: "warning",
      message: "No foreground $ task to background",
    });
  });
});

describe("registerPsCommand /ps b", () => {
  it("backgrounds foreground task without opening overlay", async () => {
    vi.mocked(backgroundForegroundBash).mockResolvedValue({ ok: true });

    let handler: ((args: string, ctx: unknown) => Promise<void>) | undefined;
    const panelHost = {} as TaskPanelHost;
    const registry = {};
    const pi = {
      registerCommand: (
        _name: string,
        spec: { handler: (args: string, ctx: unknown) => Promise<void> },
      ) => {
        handler = spec.handler;
      },
    };

    registerPsCommand(pi as never, panelHost, {} as never, {
      getRegistry: () => registry as never,
      mergeRows: () => [],
    });

    const notify = vi.fn();
    const custom = vi.fn();
    await handler!("b", {
      ui: { notify, custom },
      hasUI: true,
    });

    expect(backgroundForegroundBash).toHaveBeenCalledWith(panelHost);
    expect(notify).toHaveBeenCalledWith(USER_BASH_PANEL_BACKGROUND_MSG, "info");
    expect(custom).not.toHaveBeenCalled();
  });

  it("accepts background alias", async () => {
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

    registerPsCommand(pi as never, {} as TaskPanelHost, {} as never, {
      getRegistry: () => ({}) as never,
      mergeRows: () => [],
    });

    const notify = vi.fn();
    await handler!("background", { ui: { notify }, hasUI: true });

    expect(notify).toHaveBeenCalledWith("No foreground $ task to background", "warning");
  });
});
