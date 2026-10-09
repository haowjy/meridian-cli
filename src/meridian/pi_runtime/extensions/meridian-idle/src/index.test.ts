import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  registerMeridianIdleExtension,
  type MeridianRunner,
} from "./index";

type Handler = (event: any, ctx: ExtensionContext) => Promise<unknown> | unknown;
type CompactCallbacks = {
  onComplete?: () => void;
  onError?: (error: Error) => void;
};

function commandResult(value: unknown) {
  return {
    exitCode: 0,
    stdout: JSON.stringify(value),
    stderr: "",
  };
}

function host(run: MeridianRunner) {
  const handlers = new Map<string, Handler>();
  const pi = {
    on: (name: string, handler: Handler) => handlers.set(name, handler),
  } as unknown as ExtensionAPI;
  const runtime = registerMeridianIdleExtension(pi, run);
  return { handlers, runtime };
}

function context() {
  const state = {
    idle: true,
    pending: false,
    editor: "",
    tokens: 42_000 as number | null,
    compact: vi.fn<(options?: CompactCallbacks) => void>(),
  };
  const ctx = {
    cwd: "/work/project",
    model: { provider: "anthropic" },
    sessionManager: { getSessionId: () => "pi-session" },
    ui: { getEditorText: () => state.editor },
    isIdle: () => state.idle,
    hasPendingMessages: () => state.pending,
    getContextUsage: () => ({
      tokens: state.tokens,
      contextWindow: 200_000,
      percent: 21,
    }),
    compact: state.compact,
  } as unknown as ExtensionContext;
  return { ctx, state };
}

async function event(
  handlers: Map<string, Handler>,
  name: string,
  payload: Record<string, unknown>,
  ctx: ExtensionContext,
): Promise<void> {
  await handlers.get(name)!(payload, ctx);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("meridian idle event mapping", () => {
  it("stays inert when interactive idle policy is disabled", async () => {
    const run = vi.fn<MeridianRunner>(async () => commandResult({ enabled: false }));
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(0);

    expect(run.mock.calls.map(([args]) => args)).toEqual([
      ["idle", "config", "--interactive"],
    ]);
  });

  it("arms on idle agent_end and returns only for interactive input", async () => {
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") return commandResult([]);
      if (args[1] === "arm") {
        return commandResult({ stretch: 3, anchor: 4, push_at: Date.now() + 100 });
      }
      return commandResult({ stretch_closed: true, was_open: true });
    });
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(0);
    await event(handlers, "input", { source: "rpc" }, ctx);
    await event(handlers, "input", { source: "extension" }, ctx);
    await event(handlers, "input", { source: "interactive" }, ctx);
    await vi.advanceTimersByTimeAsync(100);

    expect(run.mock.calls.map(([args]) => args)).toEqual([
      ["idle", "config", "--interactive"],
      ["idle", "status", "--json"],
      [
        "idle", "arm", "--harness", "pi", "--session", "pi-session",
        "--provider", "anthropic", "--cwd", "/work/project",
      ],
      [
        "idle", "return", "--harness", "pi", "--session", "pi-session",
        "--user-prompt",
      ],
    ]);
  });

  it("maps live facts into fire and compacts only after act", async () => {
    let compactDeadline = 0;
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") return commandResult([]);
      if (args[1] === "arm") {
        compactDeadline = Date.now() + 100;
        return commandResult({
          stretch: 7,
          anchor: 8,
          push_at: Date.now() + 50,
          compact_at: compactDeadline,
        });
      }
      if (args[1] === "fire") {
        return commandResult({ decision: args[2] === "compact" ? "act" : "skip" });
      }
      return commandResult({});
    });
    const { ctx, state } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(0);
    state.editor = "half typed";
    state.pending = true;
    await vi.advanceTimersByTimeAsync(50);

    expect(run).toHaveBeenCalledWith([
      "idle", "fire", "push", "--harness", "pi", "--session", "pi-session",
      "--stretch", "7", "--anchor", "8", "--draft", "yes", "--busy",
      "--context-tokens", "42000",
    ]);
    expect(state.compact).not.toHaveBeenCalled();

    state.editor = "";
    state.pending = false;
    await vi.advanceTimersToNextTimerAsync();
    expect(Date.now()).toBe(compactDeadline);
    expect(run).toHaveBeenCalledWith([
      "idle", "fire", "compact", "--harness", "pi", "--session", "pi-session",
      "--stretch", "7", "--anchor", "8", "--draft", "no",
      "--context-tokens", "42000",
    ]);
    expect(state.compact).toHaveBeenCalledOnce();

    state.compact.mock.calls[0]![0]?.onComplete?.();
    await vi.waitFor(() => {
      expect(run).toHaveBeenCalledWith([
        "idle", "done", "compact", "--harness", "pi", "--session", "pi-session",
        "--stretch", "7", "--result", "ok",
      ]);
    });
  });

  it("reloads stored deadlines and keeps them after a window-absorbed arm", async () => {
    const deadline = Date.now() + 100;
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") {
        return commandResult([{
          harness: "pi",
          session: "pi-session",
          stretch: 11,
          anchor: 12,
          schedule: { push_at: deadline },
          done: {},
        }]);
      }
      if (args[1] === "arm") {
        return commandResult({
          stretch: 11,
          anchor: 12,
          reason: "compaction-window",
        });
      }
      return commandResult({ decision: "skip", reason: "late" });
    });
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(100);

    expect(run).toHaveBeenCalledWith([
      "idle", "fire", "push", "--harness", "pi", "--session", "pi-session",
      "--stretch", "11", "--anchor", "12", "--draft", "no",
      "--context-tokens", "42000",
    ]);
  });
});
