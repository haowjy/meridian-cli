import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { writeJsonAtomic } from "../../shared/json_file";
import { resolveBashRecordsPath } from "../../shared/pi_state_paths";
import type { BashRecord } from "../../shared/schemas";
import {
  registerMeridianIdleExtension,
  type MeridianRunner,
} from "./index";

type Handler = (event: any, ctx: ExtensionContext) => Promise<unknown> | unknown;

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
  };
  const ctx = {
    cwd: "/work/project",
    model: { provider: "anthropic" },
    sessionManager: { getSessionId: () => "pi-session" },
    isIdle: () => state.idle,
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
  for (const args of calls.filter((args) => ["arm", "return", "fire"].includes(args[1] ?? ""))) {
    expect(flag(args, "--harness")).toBe("pi");
    expect(flag(args, "--session")).toBe("pi-session");
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", path.join(tmpdir(), `meridian-pi-idle-empty-${process.pid}`));
  vi.stubEnv("MERIDIAN_SPAWN_ID", "p-test");
});

afterEach(() => {
  vi.unstubAllEnvs();
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

  it("arms every returned deadline and fires push then warn", async () => {
    const run = vi.fn<MeridianRunner>(async (args) => {
      if (args[1] === "config") return commandResult({ enabled: true });
      if (args[1] === "status") return commandResult([]);
      if (args[1] === "arm") {
        return commandResult({
          stretch: 7,
          anchor: 8,
          push_at: Date.now() + 10,
          warn_at: Date.now() + 20,
        });
      }
      return commandResult({ decision: "act" });
    });
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => {
      expect(run.mock.calls.filter(([args]) => args[1] === "arm")).toHaveLength(1);
    });
    await vi.advanceTimersByTimeAsync(20);

    const calls = run.mock.calls.map(([args]) => args);
    expect(calls.filter((args) => args[1] === "arm")).toEqual([[
      "idle", "arm", "--harness", "pi", "--session", "pi-session",
      "--provider", "anthropic", "--interactive",
    ]]);
    expect(calls.filter((args) => args[1] === "fire")).toEqual([
      [
        "idle", "fire", "push", "--harness", "pi", "--session", "pi-session",
        "--stretch", "7", "--anchor", "8", "--interactive",
      ],
      [
        "idle", "fire", "warn", "--harness", "pi", "--session", "pi-session",
        "--stretch", "7", "--anchor", "8", "--interactive",
      ],
    ]);
    expectCliContract(calls);
  });

  it("forwards a bounded snapshot of running Bash records and survives unreadable evidence", async () => {
    const root = await mkdtemp(path.join(tmpdir(), "meridian-pi-idle-"));
    vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
    vi.stubEnv("MERIDIAN_SPAWN_ID", "p-parent");

    try {
      const recordsPath = resolveBashRecordsPath();
      await mkdir(path.dirname(recordsPath), { recursive: true });
      const longCommand = `meridian spawn wait p42 --timeout 30 ${"x".repeat(600)}`;
      const record = (id: string, command: string, status: BashRecord["status"]): BashRecord => ({
        bash_id: id,
        command,
        cwd: "/work/project",
        pid: 123,
        status,
        is_background: true,
        is_tracked: true,
        exit_code: status === "running" ? null : 0,
        started_at_ms: 1,
        ended_at_ms: status === "running" ? null : 2,
        log_path: `${root}/${id}.log`,
        stdout_log_path: `${root}/${id}.stdout.log`,
        stderr_log_path: `${root}/${id}.stderr.log`,
        log_bytes: 0,
        timeout_min: 55,
        originating_bash_id: null,
      });
      const running = Array.from({ length: 18 }, (_, index) => record(
        `b-running-${index}`,
        index === 0 ? longCommand : `sleep ${index}`,
        "running",
      ));
      const finished = record("b-finished", "finished command", "exited");
      await writeJsonAtomic(recordsPath, {
        v: 1,
        spawn_id: "p-parent",
        updated_at_ms: 1,
        records: Object.fromEntries([...running, finished].map((row) => [row.bash_id, row])),
      });

      const run = vi.fn<MeridianRunner>(async (args) => {
        if (args[1] === "config") return commandResult({ enabled: true });
        if (args[1] === "status") return commandResult([]);
        return commandResult({ stretch: 1, anchor: 1 });
      });
      const { ctx } = context();
      const { handlers } = host(run);
      await event(handlers, "session_start", {}, ctx);

      await event(handlers, "agent_end", {}, ctx);
      await vi.advanceTimersByTimeAsync(0);
      await vi.waitFor(() => {
        expect(run.mock.calls.filter(([args]) => args[1] === "arm")).toHaveLength(1);
      });

      const firstArm = run.mock.calls.find(([args]) => args[1] === "arm")![0];
      const taskArg = firstArm.find((arg) => arg.startsWith("--background-tasks="));
      expect(taskArg).toBeDefined();
      const tasks = JSON.parse(taskArg!.slice("--background-tasks=".length)) as Array<{
        kind: string;
        command: string;
      }>;
      expect(tasks).toHaveLength(16);
      expect(tasks.every((task) => task.kind === "shell")).toBe(true);
      expect(tasks.map((task) => task.command)).not.toContain("finished command");
      expect(tasks[0]?.command).toBe(`${longCommand.slice(0, 509)}\n;:`);
      expect(tasks[0]?.command).toHaveLength(512);

      await writeFile(recordsPath, "{", "utf8");
      await event(handlers, "agent_end", {}, ctx);
      await vi.advanceTimersByTimeAsync(0);
      await vi.waitFor(() => {
        expect(run.mock.calls.filter(([args]) => args[1] === "arm")).toHaveLength(2);
      });

      const secondArm = run.mock.calls.filter(([args]) => args[1] === "arm")[1]![0];
      expect(secondArm.some((arg) => arg.startsWith("--background-tasks="))).toBe(false);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
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
    await vi.waitFor(() => {
      expect(run.mock.calls.filter(([args]) => args[1] === "arm")).toHaveLength(1);
    });

    const arm = run.mock.calls.find(([args]) => args[1] === "arm")?.[0];
    expect(arm).toEqual([
      "idle", "arm", "--harness", "pi", "--session", "pi-session",
      "--provider", "anthropic",
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
    await vi.waitFor(() => {
      expect(run.mock.calls.filter(([args]) => args[1] === "arm")).toHaveLength(2);
    });

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
        return commandResult({ stretch: 11, anchor: 12, reason: "duplicate-turn" });
      }
      return commandResult({ decision: "skip", reason: "late" });
    });
    const { ctx } = context();
    const { handlers } = host(run);

    await event(handlers, "session_start", {}, ctx);
    await event(handlers, "agent_end", {}, ctx);
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => {
      expect(run.mock.calls.filter(([args]) => args[1] === "arm")).toHaveLength(1);
    });
    await vi.advanceTimersByTimeAsync(100);

    const calls = run.mock.calls.map(([args]) => args);
    expect(calls.filter((args) => args[1] === "status")).toEqual([
      ["idle", "status", "--json", "--interactive"],
    ]);
    expect(calls.filter((args) => args[1] === "arm")).toEqual([[
      "idle", "arm", "--harness", "pi", "--session", "pi-session",
      "--provider", "anthropic", "--interactive",
    ]]);
    expect(calls.filter((args) => args[1] === "fire").map((args) => args[2])).toEqual(["warn"]);
    expect(calls.filter((args) => args[1] === "fire")[0]).toEqual([
      "idle", "fire", "warn", "--harness", "pi", "--session", "pi-session",
      "--stretch", "11", "--anchor", "12", "--interactive",
    ]);
    expectCliContract(calls);
  });
});
