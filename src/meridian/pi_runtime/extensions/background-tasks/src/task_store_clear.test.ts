import { afterEach, describe, expect, it } from "vitest";
import { access, mkdtemp, rm, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { createLocalBus } from "../../shared/meridian_event_bus";
import { TaskRegistry } from "./task_registry";

describe("TaskRegistry clearFinished", () => {
  let stateRoot = "";

  afterEach(async () => {
    if (stateRoot) {
      await rm(stateRoot, { recursive: true, force: true });
      stateRoot = "";
    }
  });

  it("deletes persisted task directories for finished jobs", async () => {
    stateRoot = await mkdtemp(path.join(tmpdir(), "bg-clear-"));
    const sessionId = "sess-clear";
    const taskId = "t-finished";
    const taskDir = path.join(stateRoot, "background-tasks", sessionId, "tasks", taskId);
    await mkdir(taskDir, { recursive: true });
    await writeFile(
      path.join(taskDir, "meta.json"),
      `${JSON.stringify({
        task_id: taskId,
        label: "done",
        command: "true",
        cwd: process.cwd(),
        wait_policy: "tracked",
        status: "exited",
        pid: 1,
        started_at_ms: 1,
        ended_at_ms: 2,
        exit_code: 0,
        signal: null,
        success: true,
        stdout_log_path: path.join(taskDir, "combined.log"),
        stderr_log_path: path.join(taskDir, "combined.log"),
        combined_log_path: path.join(taskDir, "combined.log"),
        log_bytes: 0,
        log_truncated: false,
        emitted_start: false,
        ingress: "background_task",
        persistent: false,
        duration_ms: 1,
        log_path: path.join(taskDir, "combined.log"),
      })}\n`,
      "utf-8",
    );

    const registry = new TaskRegistry(stateRoot, sessionId, null, createLocalBus());
    await registry.initialize();
    const removed = await registry.clearFinished();
    expect(removed).toBeGreaterThanOrEqual(1);

    await expect(access(taskDir)).rejects.toMatchObject({ code: "ENOENT" });
  });
});
