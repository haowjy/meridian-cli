import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const runMeridianCommand = vi.hoisted(() => vi.fn());
vi.mock("../../shared/meridian_cli", () => ({ runMeridianCommand }));

import { BashRuntime } from "./bash_runtime";
import managedBashExtension from "./index";

type ToolResult = { content: Array<{ type: string; text?: string }>; details?: unknown; isError?: boolean };
type RegisteredTool = { execute: (...args: unknown[]) => Promise<ToolResult> };

const roots: string[] = [];

afterEach(async () => {
  vi.unstubAllEnvs();
  runMeridianCommand.mockReset();
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true });
});

async function runtimeFor(id: string): Promise<{ root: string; runtime: BashRuntime }> {
  const root = await mkdtemp(path.join(tmpdir(), "pi-bash-outcomes-"));
  roots.push(root);
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", id);
  return { root, runtime: new BashRuntime() };
}

function jsonResult(payload: unknown, exitCode = 0): { stdout: string; stderr: string; exitCode: number } {
  return { stdout: JSON.stringify(payload), stderr: "", exitCode };
}

describe("managed Bash tool outcomes", () => {
  it("preserves a failed child outcome instead of treating CLI exit 1 as running", async () => {
    const { runtime } = await runtimeFor("p-parent-failed");
    runMeridianCommand.mockResolvedValueOnce(jsonResult({
      any_failed: true,
      spawns: [{ spawn_id: "p123", status: "failed", exit_code: 17, failure_reason: "worker failed", report_summary: "failure summary" }],
    }, 1));

    await expect(runtime.manage({ action: "wait", bash_id: "p123" })).resolves.toMatchObject({
      bash_id: "p123",
      status: "failed",
      exit_code: 17,
      output: "failure summary",
      error: "worker failed",
    });
    await runtime.shutdown();
  });

  it("returns checkpoint outcomes as pending rather than errors", async () => {
    const { runtime } = await runtimeFor("p-parent-checkpoint");
    runMeridianCommand.mockResolvedValueOnce(jsonResult({
      any_failed: false,
      checkpoint: true,
      checkpoint_pending_ids: ["p124"],
      spawns: [{ spawn_id: "p124", status: "running", exit_code: null }],
    }));

    await expect(runtime.manage({ action: "wait", bash_id: "p124" })).resolves.toMatchObject({
      bash_id: "p124",
      status: "running",
      checkpoint: true,
      pending_ids: ["p124"],
    });
    await runtime.shutdown();
  });

  it("reports malformed or transport wait responses as query faults", async () => {
    const { runtime } = await runtimeFor("p-parent-fault");
    runMeridianCommand.mockResolvedValueOnce({ stdout: "", stderr: "connection refused", exitCode: 1 });

    await expect(runtime.manage({ action: "wait", bash_id: "p125" })).resolves.toMatchObject({
      status: "error",
      error: expect.stringContaining("invalid JSON"),
    });
    await runtime.shutdown();
  });

  it("accepts a successful structured target even when the CLI process exits non-zero", async () => {
    const { runtime } = await runtimeFor("p-parent-success");
    runMeridianCommand.mockResolvedValueOnce(jsonResult({
      any_failed: false,
      spawns: [{ spawn_id: "p126", status: "succeeded", exit_code: 0, report_summary: "done" }],
    }, 1));

    await expect(runtime.manage({ action: "wait", bash_id: "p126" })).resolves.toMatchObject({
      status: "succeeded",
      exit_code: 0,
      output: "done",
    });
    await runtime.shutdown();
  });

  it.each([
    { status: "succeeded", exit_code: 17 },
    { status: "succeeded", exit_code: "zero" },
    { status: "succeeded", exit_code: 0.5 },
    { status: "succeeded", duration_secs: -1 },
    { status: "succeeded", report_body: 42 },
    { status: "failed", exit_code: 17 },
  ])("rejects malformed or contradictory result fields: %j", async (fields) => {
    const { runtime } = await runtimeFor("p-parent-invalid");
    runMeridianCommand.mockResolvedValueOnce(jsonResult({
      any_failed: false,
      spawns: [{ spawn_id: "p128", ...fields }],
    }));
    await expect(runtime.manage({ action: "wait", bash_id: "p128" })).resolves.toMatchObject({
      status: "error",
      error: expect.any(String),
    });
    await runtime.shutdown();
  });

  it("preserves an explicitly empty full report instead of substituting a summary", async () => {
    const { runtime } = await runtimeFor("p-parent-empty-body");
    runMeridianCommand.mockResolvedValueOnce(jsonResult({
      any_failed: false,
      spawns: [{ spawn_id: "p128", status: "succeeded", exit_code: 0, report_body: "", report_summary: "old summary" }],
    }));
    await expect(runtime.manage({ action: "wait", bash_id: "p128" })).resolves.toMatchObject({ output: "" });
    await runtime.shutdown();
  });

  it("rejects an unknown target status instead of guessing that it is pending", async () => {
    const { runtime } = await runtimeFor("p-parent-unknown");
    runMeridianCommand.mockResolvedValueOnce(jsonResult({
      any_failed: false,
      spawns: [{ spawn_id: "p128", status: "mystery" }],
    }));

    await expect(runtime.manage({ action: "wait", bash_id: "p128" })).resolves.toMatchObject({
      status: "error",
      error: expect.stringContaining("unknown target status"),
    });
    await runtime.shutdown();
  });
});

describe("managed Bash native error semantics", () => {
  it("marks an empty non-zero foreground Bash result as a model-visible error", async () => {
    const { runtime } = await runtimeFor("p-parent-native-error");
    const tools = new Map<string, RegisteredTool>();
    const host = {
      registerTool: (definition: { name: string; execute: RegisteredTool["execute"] }) => tools.set(definition.name, definition),
      registerCommand: () => undefined,
      on: () => undefined,
    };
    managedBashExtension(host as unknown as ExtensionAPI);

    const result = await tools.get("bash")!.execute("call", { command: "exit 17" }, undefined);
    expect(result.isError).toBe(true);
    expect(result.content[0]?.text).toContain("exit code 17");

    runMeridianCommand.mockResolvedValueOnce(jsonResult({
      any_failed: true,
      spawns: [{ spawn_id: "p127", status: "failed", exit_code: 17, failure_reason: "worker failed" }],
    }, 1));
    const childResult = await tools.get("bash_manage")!.execute("call", { action: "wait", bash_id: "p127" });
    expect(childResult.isError).toBe(true);
    expect(childResult.content[0]?.text).toContain("p127: failed (exit code 17)");
    expect(childResult.content[0]?.text).toContain("worker failed");

    runMeridianCommand.mockResolvedValueOnce(jsonResult({
      any_failed: false,
      spawns: [{ spawn_id: "p129", status: "succeeded", exit_code: 0 }],
    }));
    const success = await tools.get("bash_manage")!.execute("call", { action: "wait", bash_id: "p129" });
    expect(success.isError).not.toBe(true);
    expect(success.content[0]?.text).toContain("p129: succeeded (exit code 0)");
    await runtime.shutdown();
  });
});
