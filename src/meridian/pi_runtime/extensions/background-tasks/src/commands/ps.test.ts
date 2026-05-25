import { describe, expect, it, vi } from "vitest";

import { backgroundForegroundBash } from "../background_foreground";
import { USER_BASH_PANEL_BACKGROUND_MSG } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";
import { psBackgroundSubcommandMessage } from "./background";
import { registerPsCommand } from "./ps";

vi.mock("../background_foreground", () => ({
  backgroundForegroundBash: vi.fn(),
}));

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

describe("registerPsCommand", () => {
  it("does not background on space arg b — opens overlay instead", async () => {
    vi.mocked(backgroundForegroundBash).mockClear();

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
    const custom = vi.fn().mockResolvedValue(null);
    await handler!("b", {
      ui: { notify, custom, setWidget: vi.fn() },
      hasUI: true,
    });

    expect(backgroundForegroundBash).not.toHaveBeenCalled();
    expect(custom).toHaveBeenCalled();
  });
});
