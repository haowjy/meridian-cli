import { describe, expect, it, vi } from "vitest";

import { setupBashBridge, splitUserBashBackground } from "./bash_bridge";
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
  });
});
