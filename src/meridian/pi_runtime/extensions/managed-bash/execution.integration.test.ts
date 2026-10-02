import { mkdir, mkdtemp, open, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";

import { BashRuntime } from "./src/bash_runtime";
import { readLogTail } from "./src/bash_log_store";
import managedBashExtension from "./src/index";
import type { BashRecord } from "../shared/schemas";

const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
async function until(fn: () => boolean | Promise<boolean>): Promise<void> {
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) { if (await fn()) return; await pause(10); }
  throw new Error("Expected task observation did not arrive");
}

async function setup(): Promise<{ root: string; runtime: BashRuntime; recordsPath: string }> {
  const root = await mkdtemp(path.join(tmpdir(), "pi-execution-"));
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", "p900001");
  const runtime = new BashRuntime();
  return { root, runtime, recordsPath: path.join(root, "pi-bash", "p900001", "bash-records.json") };
}

afterEach(() => vi.unstubAllEnvs());

describe("managed Bash execution ownership", () => {
  it("keeps escaped ampersands in foreground user Bash commands", async () => {
    const { root, runtime } = await setup();
    const hooks = new Map<string, Function>();
    const host = {on: (name: string, fn: Function) => hooks.set(name, fn), registerTool() {}, registerCommand() {}};
    try {
      managedBashExtension(host as unknown as ExtensionAPI);
      const result = await hooks.get("user_bash")!({command: "printf '%s' \\&", cwd: root});
      expect(result.operations).toBeDefined();
      expect(result.result).toBeUndefined();
      expect(runtime.list(true)).toEqual([]);
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("routes noninteractive slash-command output through the native UI API", async () => {
    const { root, runtime } = await setup();
    const commands = new Map<string, Parameters<ExtensionAPI["registerCommand"]>[1]>();
    const host = {
      on: () => {}, registerTool: () => {},
      registerCommand: (name: string, command: Parameters<ExtensionAPI["registerCommand"]>[1]) => commands.set(name, command),
    };
    try {
      managedBashExtension(host as unknown as ExtensionAPI);
      await runtime.execute({ command: "printf rpc-safe" }, undefined);
      const row = runtime.list(true)[0]!;
      const notify = vi.fn();
      const ctx = { hasUI: false, ui: { notify } } as unknown as ExtensionCommandContext;
      const stdout = vi.spyOn(process.stdout, "write").mockImplementation(() => true);
      try {
        await commands.get("ps")!.handler("", ctx);
        await commands.get("ps:logs")!.handler(row.bash_id, ctx);
        expect(stdout).not.toHaveBeenCalled();
      } finally { stdout.mockRestore(); }
      expect(notify).toHaveBeenCalledWith(expect.stringContaining(row.bash_id), "info");
      expect(notify).toHaveBeenCalledWith("rpc-safe", "info");
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("clears returned foreground results without needing notification receipts", async () => {
    const { root, runtime } = await setup();
    try {
      await runtime.execute({ command: "printf foreground" }, undefined);
      expect(await runtime.clearFinished()).toBe(1);
      expect(runtime.list(true)).toEqual([]);
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("preserves unattended terminal obligations when clearing completed history", async () => {
    const { root, runtime } = await setup();
    try {
      const started = await runtime.execute({ command: "printf unattended", background: true }, undefined) as { bash_id: string };
      await until(() => runtime.list(true)[0]?.status === "exited");
      expect(await runtime.clearFinished()).toBe(0);
      expect(await runtime.manage({ action: "wait", bash_id: started.bash_id })).toMatchObject({ status: "exited", output: "unattended" });
      expect(await runtime.clearFinished()).toBe(1);
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("clears causally admitted completion and refuses invalid receipt evidence", async () => {
    const { root, runtime, recordsPath } = await setup();
    try {
      const started = await runtime.execute({ command: "printf admitted", background: true }, undefined) as { bash_id: string };
      await until(() => runtime.list(true)[0]?.status === "exited");
      const receiptsPath = path.join(path.dirname(recordsPath), "delivery-receipts.json");
      await writeFile(receiptsPath, "{");
      await expect(runtime.clearFinished()).rejects.toThrow();
      await writeFile(receiptsPath, JSON.stringify({ v: 1, spawn_id: "different-parent", messages: {} }));
      await expect(runtime.clearFinished()).rejects.toThrow("invalid delivery receipts");
      expect(runtime.list(true)).toHaveLength(1);
      await writeFile(receiptsPath, JSON.stringify({ v: 1, spawn_id: "p900001", messages: { "delivery-1": [started.bash_id] } }));
      vi.stubEnv("_MERIDIAN_PI_STATE_DIR", path.join(root, "later-env"));
      expect(await runtime.clearFinished()).toBe(1);
      expect(JSON.parse(await readFile(recordsPath, "utf8")).records).toEqual({});
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("abort waits for foreground task exit", async () => {
    const { root, runtime } = await setup();
    try {
      const controller = new AbortController();
      const result = runtime.execute({ command: "sleep 5" }, controller.signal);
      await until(() => runtime.list(true).length === 1);
      controller.abort();
      expect(await result).toMatchObject({ exit_code: -1 });
      expect(runtime.list(true)[0]?.status).toBe("killed");
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("shutdown cleans owned tasks even when detached from quiescence", async () => {
    const { root, runtime } = await setup();
    try {
      const started = await runtime.execute({ command: "sleep 5", background: true }, undefined) as { bash_id: string };
      await runtime.manage({ action: "detach", bash_id: started.bash_id });
      await runtime.shutdown();
      expect(runtime.list(true)[0]).toMatchObject({ status: "killed", is_tracked: false });
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("kills a TERM-ignoring child before publishing terminal", async () => {
    const { root, runtime } = await setup();
    let workerPid: number | undefined;
    try {
      const worker = path.join(root, "worker.cjs");
      const pidFile = path.join(root, "pid");
      const pulse = path.join(root, "pulse");
      await writeFile(worker, `const fs=require('fs');fs.writeFileSync(${JSON.stringify(pidFile)},String(process.pid));process.on('SIGTERM',()=>{});setInterval(()=>fs.appendFileSync(${JSON.stringify(pulse)},'.'),15);setTimeout(()=>process.exit(0),4000);`);
      const started = await runtime.execute({ command: `'${process.execPath}' '${worker}' & wait`, background: true }, undefined) as { bash_id: string };
      await until(async () => { try { workerPid = Number(await readFile(pidFile, "utf8")); return true; } catch { return false; } });
      await until(async () => { try { return (await readFile(pulse)).length > 0; } catch { return false; } });
      const killed = await runtime.manage({ action: "kill", bash_id: started.bash_id });
      expect(killed).toMatchObject({ killed: true });
      const count = (await readFile(pulse)).length;
      await pause(60);
      expect((await readFile(pulse)).length).toBe(count);
      expect(runtime.list(true).find((row) => row.bash_id === started.bash_id)?.status).toBe("killed");
    } finally {
      if (workerPid) { try { process.kill(workerPid, "SIGKILL"); } catch {} }
      await runtime.shutdown();
      await rm(root, { recursive: true, force: true });
    }
  });

  it("retains the live task owner across native reload", async () => {
    const { root, runtime } = await setup();
    const hooks = new Map<string, (event: unknown) => Promise<void>>();
    const host = { on: (name: string, fn: (event: unknown) => Promise<void>) => hooks.set(name, fn), registerTool: () => {}, registerCommand: () => {} };
    try {
      managedBashExtension(host as unknown as Parameters<typeof managedBashExtension>[0]);
      const started = await runtime.execute({ command: "sleep 2", background: true }, undefined) as { bash_id: string };
      await hooks.get("session_shutdown")!({ reason: "reload" });
      managedBashExtension(host as unknown as Parameters<typeof managedBashExtension>[0]);
      const rebuilt = new BashRuntime();
      expect((await rebuilt.manage({ action: "list", include_completed: true }))).toMatchObject({ rows: [expect.objectContaining({ bash_id: started.bash_id, status: "running" })] });
      expect(await rebuilt.manage({ action: "kill", bash_id: started.bash_id })).toMatchObject({ killed: true });
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("recovers terminal receipts and exposes lost ownership without signalling persisted PIDs", async () => {
    const root = await mkdtemp(path.join(tmpdir(), "pi-execution-recovery-"));
    vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root); vi.stubEnv("MERIDIAN_SPAWN_ID", "p900001");
    const recordsPath = path.join(root, "pi-bash", "p900001", "bash-records.json");
    const base: BashRecord = { bash_id: "b-12345678", command: "old task", cwd: root, pid: process.pid, status: "running", is_background: true, is_tracked: true, exit_code: null, started_at_ms: Date.now() - 1000, ended_at_ms: null, log_path: path.join(root, "old.log"), stdout_log_path: path.join(root, "old.out"), stderr_log_path: path.join(root, "old.err"), log_bytes: 0, timeout_min: 55, originating_bash_id: null };
    const terminal = { ...base, bash_id: "b-23456789", status: "exited", exit_code: 0, ended_at_ms: Date.now(), notification_consumed_at_ms: 1234 };
    await mkdir(path.dirname(recordsPath), { recursive: true });
    await writeFile(recordsPath, JSON.stringify({ v: 1, spawn_id: "p900001", updated_at_ms: Date.now(), records: { [base.bash_id]: base, [terminal.bash_id]: terminal } }));
    const runtime = new BashRuntime();
    try {
      const listed = await runtime.manage({ action: "list", include_completed: true });
      expect(listed).toMatchObject({ rows: expect.arrayContaining([expect.objectContaining({ bash_id: base.bash_id, execution_error: expect.stringContaining("ownership_lost") }), expect.objectContaining({ bash_id: terminal.bash_id, status: "exited" })]) });
      expect(await runtime.manage({ action: "kill", bash_id: base.bash_id })).toMatchObject({ killed: false });
      await expect(runtime.manage({ action: "wait", bash_id: base.bash_id })).rejects.toThrow("ownership_lost");
      expect(await runtime.manage({ action: "detach", bash_id: base.bash_id })).toMatchObject({ detached: true });
      await runtime.execute({ command: "printf fresh" }, undefined);
      const recovered = JSON.parse(await readFile(recordsPath, "utf8"));
      expect(recovered.records[terminal.bash_id].notification_consumed_at_ms).toBe(1234);
      expect(recovered.records[base.bash_id].is_tracked).toBe(false);
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("supervises output storage failure and cleans the task before returning an error", async () => {
    const { root, runtime, recordsPath } = await setup();
    const unhandled: unknown[] = [];
    const listener = (error: unknown): void => { unhandled.push(error); };
    process.on("unhandledRejection", listener);
    try {
      const started = await runtime.execute({ command: "sleep 0.1; head -c 8388608 /dev/zero; sleep 2", background: true }, undefined) as { bash_id: string };
      await rm(recordsPath); await mkdir(recordsPath);
      await until(() => runtime.list(true).some((row) => row.bash_id === started.bash_id && row.execution_error != null));
      await until(() => runtime.list(true).some((row) => row.bash_id === started.bash_id && row.status === "killed"));
      await pause(30);
      expect(unhandled).toEqual([]);
      expect(runtime.list(true).find((row) => row.bash_id === started.bash_id)?.execution_error).not.toContain("cleanup failed");
      await rm(recordsPath, { recursive: true });
      await expect(runtime.manage({ action: "wait", bash_id: started.bash_id })).rejects.toThrow("EISDIR");
      expect(JSON.parse(await readFile(recordsPath, "utf8")).records[started.bash_id].notification_consumed_at_ms).toBeUndefined();
      const result = await runtime.execute({ command: "printf recovered" }, undefined);
      expect(result).toMatchObject({ stdout: "recovered", exit_code: 0 });
      expect(JSON.parse(await readFile(recordsPath, "utf8")).runtime_error).toBeUndefined();
    } finally {
      process.removeListener("unhandledRejection", listener);
      await rm(recordsPath, { recursive: true, force: true });
      await runtime.shutdown(); await rm(root, { recursive: true, force: true });
    }
  });

  it("cleans newly started work when its first publication fails", async () => {
    const { root, runtime, recordsPath } = await setup();
    try {
      await runtime.rows();
      await mkdir(recordsPath, { recursive: true });
      await expect(runtime.execute({ command: "sleep 5", background: true }, undefined)).rejects.toThrow();
      const [row] = runtime.list(true);
      expect(row?.status).toBe("killed");
      expect(row?.execution_error).not.toContain("cleanup failed");
      if (row?.pid) expect(() => process.kill(-row.pid!, 0)).toThrow();
    } finally {
      await rm(recordsPath, { recursive: true, force: true });
      await runtime.shutdown(); await rm(root, { recursive: true, force: true });
    }
  });

  it("tails a large sparse log within a byte bound", async () => {
    const root = await mkdtemp(path.join(tmpdir(), "pi-log-tail-"));
    const filePath = path.join(root, "large.log");
    try {
      const file = await open(filePath, "w");
      try { await file.write(Buffer.from("final €\n"), 0, Buffer.byteLength("final €\n"), 256 * 1024 * 1024); }
      finally { await file.close(); }
      const tail = await readLogTail(filePath, 32);
      expect(Buffer.byteLength(tail)).toBeLessThanOrEqual(32);
      expect(tail).toMatch(/final €\n$/);
    } finally { await rm(root, { recursive: true, force: true }); }
  });
});
