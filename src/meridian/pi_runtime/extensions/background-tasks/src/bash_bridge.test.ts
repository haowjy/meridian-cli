import { describe, expect, it, vi } from "vitest";

import {
  getForegroundUserBashTaskId,
  setForegroundUserBashTaskId,
  setOnAgentBashRunning,
  setupBashBridge,
  splitUserBashBackground,
} from "./bash_bridge";
import type { TaskRegistry } from "./task_registry";

describe("splitUserBashBackground", () => {
  it("detects trailing & and strips it", () => {
    expect(splitUserBashBackground("sleep 1 &")).toEqual({
      background: true,
      execCommand: "sleep 1",
    });
  });

  it("ignores & inside quotes", () => {
    expect(splitUserBashBackground('echo "a &"')).toEqual({
      background: false,
      execCommand: 'echo "a &"',
    });
  });

  it("rejects bare &", () => {
    expect(splitUserBashBackground("&")).toEqual({
      background: false,
      execCommand: "&",
    });
  });
});

describe("setupBashBridge", () => {
  it("registers running bash jobs into the task registry", async () => {
    const syncBashToolRunning = vi.fn(async () => ({ task_id: "job-1" }));
    const syncBashToolExited = vi.fn(async () => null);
    const registry = {
      syncBashToolRunning,
      syncBashToolExited,
    } as unknown as TaskRegistry;

    const handlers = new Map<string, (event: unknown) => unknown>();
    const pi = {
      on: (event: string, handler: (event: unknown) => unknown) => {
        handlers.set(event, handler);
      },
    };

    setupBashBridge(pi, { registry });

    const toolResult = handlers.get("tool_result");
    expect(toolResult).toBeDefined();

    await toolResult?.({
      toolName: "bash",
      details: {
        state: "running",
        job_id: "job-1",
        pid: 4242,
        command: "sleep 1000",
        wait_policy: "detached",
      },
    });

    expect(syncBashToolRunning).toHaveBeenCalledWith(
      expect.objectContaining({
        taskId: "job-1",
        command: "sleep 1000",
        pid: 4242,
      }),
    );

    await toolResult?.({
      toolName: "bash",
      details: {
        state: "exited",
        job_id: "job-1",
        exit_code: 0,
      },
    });

    expect(syncBashToolExited).toHaveBeenCalledWith(
      expect.objectContaining({
        taskId: "job-1",
        exitCode: 0,
      }),
    );
  });

  it("reads Pi async bash details.jobId", async () => {
    const syncBashToolRunning = vi.fn(async () => null);
    const registry = { syncBashToolRunning, syncBashToolExited: vi.fn() } as unknown as TaskRegistry;
    const handlers = new Map<string, (event: unknown) => unknown>();
    setupBashBridge(
      { on: (event, handler) => handlers.set(event, handler) },
      { registry },
    );

    await handlers.get("tool_result")?.({
      toolName: "bash",
      input: { command: "sleep 9" },
      details: {
        async: { state: "running", jobId: "async-job-1", type: "bash" },
      },
    });

    expect(syncBashToolRunning).toHaveBeenCalledWith(
      expect.objectContaining({ taskId: "async-job-1", command: "sleep 9" }),
    );
  });

  it("notifies agent bash running only for tracked wait_policy", async () => {
    const registry = {
      syncBashToolRunning: vi.fn(async () => null),
      syncBashToolExited: vi.fn(),
    } as unknown as TaskRegistry;
    const handlers = new Map<string, (event: unknown) => unknown>();
    setupBashBridge(
      { on: (event, handler) => handlers.set(event, handler) },
      { registry },
    );

    const onRunning = vi.fn();
    setOnAgentBashRunning(onRunning);

    await handlers.get("tool_result")?.({
      toolName: "bash",
      details: { state: "running", job_id: "det-1", wait_policy: "detached" },
    });
    expect(onRunning).not.toHaveBeenCalled();

    await handlers.get("tool_result")?.({
      toolName: "bash",
      details: { state: "running", job_id: "trk-1", wait_policy: "tracked" },
    });
    expect(onRunning).toHaveBeenCalledWith("trk-1", "tracked");

    setOnAgentBashRunning(null);
  });

  it("returns user_bash result immediately for trailing &", async () => {
    const startJob = vi.fn(async () => ({
      runtimeJob: { record: { task_id: "t-bg" } },
    }));
    const detachJob = vi.fn(async () => null);
    const waitForCompletion = vi.fn();
    const registry = {
      startJob,
      detachJob,
      waitForCompletion,
      killJob: vi.fn(),
    } as unknown as TaskRegistry;

    const handlers = new Map<string, (event: unknown) => unknown>();
    setupBashBridge(
      { on: (event, handler) => handlers.set(event, handler) },
      { registry },
    );

    const hook = await handlers.get("user_bash")?.({
      type: "user_bash",
      command: "sleep 1 &",
      cwd: "/tmp",
    });

    expect(hook).toHaveProperty("result");
    expect(hook).not.toHaveProperty("operations");
    expect(hook.result.exitCode).toBe(0);
    expect(hook.result.output).toContain("t-bg");
    expect(startJob).toHaveBeenCalledWith(
      "sleep 1",
      "detached",
      "/tmp",
      expect.any(Object),
      undefined,
      expect.objectContaining({ ingress: "bash" }),
    );
    expect(detachJob).toHaveBeenCalledWith("t-bg");
    expect(waitForCompletion).not.toHaveBeenCalled();
  });

  it("returns user_bash operations that register tasks", async () => {
    const startJob = vi.fn(async () => ({
      runtimeJob: { record: { task_id: "t-user" } },
    }));
    const detachJob = vi.fn(async () => null);
    const waitForCompletion = vi.fn(async () => ({ exit_code: 0 }));
    const killJob = vi.fn();
    const registry = {
      startJob,
      detachJob,
      waitForCompletion,
      killJob,
    } as unknown as TaskRegistry;

    const handlers = new Map<string, (event: unknown) => unknown>();
    setupBashBridge(
      { on: (event, handler) => handlers.set(event, handler) },
      { registry },
    );

    const hook = await handlers.get("user_bash")?.({
      type: "user_bash",
      command: "sleep 1",
      cwd: "/tmp",
    });

    expect(hook).toHaveProperty("operations.exec");
    expect(hook).not.toHaveProperty("result");
    await hook.operations.exec("sleep 1", "/tmp", { timeout: 5 });

    expect(startJob).toHaveBeenCalled();
    expect(detachJob).toHaveBeenCalledWith("t-user");
    expect(waitForCompletion).toHaveBeenCalledWith("t-user", 5000);
    expect(getForegroundUserBashTaskId()).toBeNull();
  });

  it("tracks foreground task id while user_bash exec is waiting", async () => {
    let resolveWait!: () => void;
    const waitForCompletion = vi.fn(
      () =>
        new Promise<{ exit_code: number }>((resolve) => {
          resolveWait = () => resolve({ exit_code: 0 });
        }),
    );
    const registry = {
      startJob: vi.fn(async () => ({
        runtimeJob: { record: { task_id: "t-fg" } },
      })),
      detachJob: vi.fn(async () => null),
      waitForCompletion,
      killJob: vi.fn(),
    } as unknown as TaskRegistry;

    const handlers = new Map<string, (event: unknown) => unknown>();
    setupBashBridge(
      { on: (event, handler) => handlers.set(event, handler) },
      { registry },
    );

    const hook = await handlers.get("user_bash")?.({
      type: "user_bash",
      command: "sleep 9",
      cwd: "/tmp",
    });
    const execPromise = hook.operations.exec("sleep 9", "/tmp", { timeout: 60 });
    await vi.waitFor(() => {
      expect(getForegroundUserBashTaskId()).toBe("t-fg");
    });
    resolveWait();
    await execPromise;
    expect(getForegroundUserBashTaskId()).toBeNull();
  });

  it("returns exit 0 when foreground wait is released from /ps", async () => {
    let releaseWait!: () => void;
    const waitForCompletion = vi.fn(
      () =>
        new Promise<{ status: string; exit_code: number | null }>((resolve) => {
          releaseWait = () => resolve({ status: "running", exit_code: null });
        }),
    );
    const onData = vi.fn();
    const registry = {
      startJob: vi.fn(async () => ({
        runtimeJob: { record: { task_id: "t-bg-panel" } },
      })),
      detachJob: vi.fn(async () => null),
      waitForCompletion,
      killJob: vi.fn(),
    } as unknown as TaskRegistry;

    const handlers = new Map<string, (event: unknown) => unknown>();
    setupBashBridge(
      { on: (event, handler) => handlers.set(event, handler) },
      { registry },
    );

    const hook = await handlers.get("user_bash")?.({
      type: "user_bash",
      command: "sleep 99",
      cwd: "/tmp",
    });
    const execPromise = hook.operations.exec("sleep 99", "/tmp", {
      timeout: 120,
      onData,
    });
    await vi.waitFor(() => {
      expect(getForegroundUserBashTaskId()).toBe("t-bg-panel");
    });
    setForegroundUserBashTaskId(null);
    releaseWait();
    const result = await execPromise;
    expect(result.exitCode).toBe(0);
    expect(onData).toHaveBeenCalled();
    expect(getForegroundUserBashTaskId()).toBeNull();
  });
});
