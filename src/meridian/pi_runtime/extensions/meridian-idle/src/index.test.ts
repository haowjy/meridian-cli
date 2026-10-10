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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function commandResult(value: unknown) {
  return { exitCode: 0, stdout: JSON.stringify(value), stderr: "" };
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
    compact: vi.fn<(options?: CompactCallbacks) => void>(),
  };
  const ctx = {
    cwd: "/work/project",
    model: { provider: "anthropic" },
    sessionManager: { getSessionId: () => "pi-session" },
    ui: { getEditorText: () => state.editor },
    isIdle: () => state.idle,
    hasPendingMessages: () => state.pending,
    getContextUsage: () => ({ tokens: 42_000, contextWindow: 200_000, percent: 21 }),
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

function flag(args: string[], name: string): string | undefined {
  const at = args.indexOf(name);
  return at < 0 ? undefined : args[at + 1];
}

function expectCliContract(calls: string[][]): void {
  expect(calls.every((args) => args.at(-1) === "--interactive")).toBe(true);
  for (const args of calls.filter((args) => ["arm", "return", "fire", "done"].includes(args[1] ?? ""))) {
    expect(flag(args, "--harness")).toBe("pi");
    expect(flag(args, "--session")).toBe("pi-session");
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("meridian idle event mapping", () => {
  it("stays inert when the interactive policy gate disables it", async () => {
    const run = vi.fn<MeridianRunner>(async () => commandResult({ enabled: false }));
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await event(handlers, "input", { source: "interactive" }, ctx);
    await vi.advanceTimersByTimeAsync(0);

    expect(run.mock.calls.map(([args]) => args)).toEqual([
      ["idle", "config", "--interactive"],
    ]);
  });

  it("arms every returned deadline and reports a successful compaction", async () => {
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") return commandResult([]);
      if (args[1] === "arm") {
        return commandResult({
          stretch: 7,
          anchor: 8,
          push_at: Date.now() + 10,
          warn_at: Date.now() + 20,
          compact_at: Date.now() + 30,
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
    await vi.advanceTimersByTimeAsync(30);

    const calls = run.mock.calls.map(([args]) => args);
    expect(calls.filter((args) => args[1] === "arm")).toEqual([[
      "idle", "arm", "--harness", "pi", "--session", "pi-session",
      "--provider", "anthropic", "--cwd", "/work/project", "--interactive",
    ]]);
    expect(calls.filter((args) => args[1] === "fire").map((args) => args[2])).toEqual([
      "push", "warn", "compact",
    ]);
    expect(calls.filter((args) => args[1] === "fire" && args[2] === "compact")).toEqual([[
      "idle", "fire", "compact", "--harness", "pi", "--session", "pi-session",
      "--stretch", "7", "--anchor", "8", "--draft", "no",
      "--context-tokens", "42000", "--interactive",
    ]]);
    expect(state.compact).toHaveBeenCalledOnce();

    state.compact.mock.calls[0]![0]?.onComplete?.();
    await vi.waitFor(() => {
      expect(run).toHaveBeenCalledWith([
        "idle", "done", "compact", "--harness", "pi", "--session", "pi-session",
        "--stretch", "7", "--result", "ok", "--interactive",
      ]);
    });
    expectCliContract(run.mock.calls.map(([args]) => args));
  });

  it("extracts excerpts and passes dash-leading text in equals-form argv", async () => {
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") return commandResult([]);
      return commandResult({ stretch: 1, anchor: 1 });
    });
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {
      messages: [
        { role: "user", content: [{ type: "text", text: "old prompt" }] },
        { role: "assistant", content: [{ type: "text", text: "old answer" }] },
        { role: "toolResult", content: [{ type: "text", text: "tool output" }] },
        { role: "user", content: [{ type: "text", text: "latest\n prompt" }] },
        {
          role: "assistant",
          content: [
            { type: "thinking", thinking: "private" },
            { type: "text", text: "- latest answer" },
          ],
        },
      ],
    }, ctx);
    await vi.advanceTimersByTimeAsync(0);

    const arm = run.mock.calls.find(([args]) => args[1] === "arm")?.[0];
    expect(arm).toEqual([
      "idle", "arm", "--harness", "pi", "--session", "pi-session",
      "--provider", "anthropic", "--cwd", "/work/project",
      "--user-text=latest\n prompt", "--assistant-text=- latest answer",
      "--interactive",
    ]);
    expect(arm).not.toContain("tool output");
  });

  it("keeps the last user text when an injected run has no user message", async () => {
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") return commandResult([]);
      return commandResult({ stretch: 1, anchor: 1 });
    });
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {
      messages: [
        { role: "user", content: [{ type: "text", text: "original prompt" }] },
        { role: "assistant", content: [{ type: "text", text: "first answer" }] },
      ],
    }, ctx);
    await event(handlers, "agent_end", {
      messages: [
        { role: "custom", content: [{ type: "text", text: "spawn finished" }] },
        { role: "assistant", content: [{ type: "text", text: "follow-up answer" }] },
      ],
    }, ctx);
    await vi.advanceTimersByTimeAsync(0);

    const arms = run.mock.calls
      .map(([args]) => args)
      .filter((args) => args[1] === "arm");
    expect(arms).toHaveLength(2);
    expect(arms[1]).toContain("--user-text=original prompt");
    expect(arms[1]).toContain("--assistant-text=follow-up answer");
  });

  it("only interactive input returns and clears the pending timers", async () => {
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") return commandResult([]);
      if (args[1] === "arm") {
        return commandResult({ stretch: 3, anchor: 4, push_at: Date.now() + 100 });
      }
      return commandResult({ stretch_closed: true });
    });
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(0);
    await event(handlers, "input", { source: "rpc" }, ctx);
    expect(run.mock.calls.some(([args]) => args[1] === "return")).toBe(false);

    await event(handlers, "input", { source: "interactive" }, ctx);
    await vi.waitFor(() => {
      expect(run).toHaveBeenCalledWith([
        "idle", "return", "--harness", "pi", "--session", "pi-session",
        "--user-prompt", "--interactive",
      ]);
    });
    await vi.advanceTimersByTimeAsync(100);
    const calls = run.mock.calls.map(([args]) => args);
    expect(calls.filter((args) => args[1] === "fire")).toEqual([]);
    expectCliContract(calls);
  });

  it("serializes return behind an in-flight arm", async () => {
    const pendingArm = deferred<ReturnType<typeof commandResult>>();
    const armStarted = deferred<void>();
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") return commandResult([]);
      if (args[1] === "arm") {
        armStarted.resolve();
        return pendingArm.promise;
      }
      return commandResult({ stretch_closed: true });
    });
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(0);
    await armStarted.promise;

    expect(handlers.get("input")!({ source: "interactive" }, ctx)).toBeUndefined();
    await Promise.resolve();
    expect(run.mock.calls.filter(([args]) => ["arm", "return"].includes(args[1]!)).map(([args]) => args[1])).toEqual(["arm"]);

    pendingArm.resolve(commandResult({ stretch: 1, anchor: 1 }));
    await vi.waitFor(() => {
      expect(run.mock.calls.filter(([args]) => ["arm", "return"].includes(args[1]!)).map(([args]) => args[1])).toEqual(["arm", "return"]);
    });
  });

  it.each([
    {
      condition: "a draft appeared",
      reason: "draft",
      change: (_runtime: ReturnType<typeof host>["runtime"], _ctx: ExtensionContext, state: ReturnType<typeof context>["state"]) => {
        state.editor = "new draft";
      },
    },
  ])("vetoes compaction after act when $condition", async ({ reason, change }) => {
    let runtime!: ReturnType<typeof host>["runtime"];
    const { ctx, state } = context();
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") return commandResult([]);
      if (args[1] === "arm") {
        return commandResult({ stretch: 7, anchor: 8, compact_at: Date.now() + 100 });
      }
      if (args[1] === "fire") {
        change(runtime, ctx, state);
        return commandResult({ decision: "act" });
      }
      return commandResult({});
    });
    const installed = host(run);
    runtime = installed.runtime;

    await event(installed.handlers, "session_start", {}, ctx);
    await event(installed.handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(100);

    expect(state.compact).not.toHaveBeenCalled();
    const done = run.mock.calls.find(([args]) => args[1] === "done")?.[0];
    expect(done).toEqual([
      "idle", "done", "compact", "--harness", "pi", "--session", "pi-session",
      "--stretch", "7", "--result", "vetoed", "--reason", reason, "--interactive",
    ]);
  });

  it("reload recovery skips completed stages and an absorbed arm keeps its timers", async () => {
    const deadline = Date.now() + 100;
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") {
        return commandResult([{
          harness: "pi",
          session: "pi-session",
          stretch: 11,
          anchor: 12,
          schedule: { push_at: Date.now() + 50, warn_at: deadline },
          done: { push: "sent" },
        }]);
      }
      if (args[1] === "arm") {
        return commandResult({ stretch: 11, anchor: 12, reason: "compaction-window" });
      }
      return commandResult({ decision: "skip", reason: "late" });
    });
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(100);

    const calls = run.mock.calls.map(([args]) => args);
    expect(calls.filter((args) => args[1] === "status")).toEqual([
      ["idle", "status", "--json", "--interactive"],
    ]);
    expect(calls.filter((args) => args[1] === "arm")).toEqual([[
      "idle", "arm", "--harness", "pi", "--session", "pi-session",
      "--provider", "anthropic", "--cwd", "/work/project", "--interactive",
    ]]);
    expect(calls.filter((args) => args[1] === "fire").map((args) => args[2])).toEqual(["warn"]);
    expect(calls.filter((args) => args[1] === "fire")[0]).toEqual([
      "idle", "fire", "warn", "--harness", "pi", "--session", "pi-session",
      "--stretch", "11", "--anchor", "12", "--draft", "no",
      "--context-tokens", "42000", "--interactive",
    ]);
    expectCliContract(calls);
  });
});
