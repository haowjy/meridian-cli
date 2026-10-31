import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
import { expect, it, vi } from "vitest";
import type { BashRecord, BashRecordsFile } from "../shared/schemas";
import extension from "./src/index";

type ToolResult = { content: Array<{ text?: string }>; details: unknown; isError?: boolean };
type Tool = { execute: (id: string, params: unknown) => Promise<ToolResult> };

it.each((["output", "wait"] as const).flatMap((action) =>
  (["empty", "missing", "unreadable", "nonzero", "fault", "empty_fault", "unknown_exit"] as const)
    .map((variant) => ({ action, variant }))))(
  "returns the observed outcome and log availability before acknowledging $variant $action",
  async ({ action, variant }) => {
  const root = await mkdtemp(path.join(tmpdir(), "bash-result-read-"));
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", "p9000");
  const dir = path.join(root, "pi-bash", "p9000");
  await mkdir(dir, { recursive: true });
  const log = variant === "unreadable" ? root : path.join(root, "output.log");
  if (variant !== "missing" && variant !== "unreadable") await writeFile(log, "");
  const hasFault = variant === "fault" || variant === "empty_fault";
  const record: BashRecord = {
    bash_id: "b-00000001", command: "synthetic terminal fixture; not executed", cwd: root, pid: null,
    status: hasFault ? "killed" : "exited",
    exit_code: variant === "nonzero" ? 2 : hasFault ? -1 : variant === "unknown_exit" ? null : 0,
    is_background: true, is_tracked: true, started_at_ms: 1, ended_at_ms: 2,
    log_path: log, stdout_log_path: log, stderr_log_path: log, log_bytes: 0, timeout_min: 55,
   
    ...(variant === "fault" ? { execution_error: "EIO: failed publication" } : {}),
    ...(variant === "empty_fault" ? { execution_error: "" } : {}),
  };
  const recordsPath = path.join(dir, "bash-records.json");
  await writeFile(recordsPath, JSON.stringify({ v: 1, spawn_id: "p9000", updated_at_ms: 1, records: { [record.bash_id]: record } }));
  const tools = new Map<string, Tool>();
  let shutdown: (() => Promise<void>) | undefined;
  extension({
    registerTool: (tool: Tool & { name: string }) => tools.set(tool.name, tool), registerCommand: () => {},
    on: (name: string, callback: (event: { reason: string }) => Promise<void>) => { if (name === "session_shutdown") shutdown = () => callback({ reason: "quit" }); },
  } as unknown as ExtensionAPI);
  try {
    const result = await tools.get("bash_manage")!.execute("read", { action, bash_id: record.bash_id });
    expect(result.details).toMatchObject({ kind: action, status: record.status, exit_code: record.exit_code,
      log_state: variant === "missing" || variant === "unreadable" ? variant : "available" });
    expect(Boolean(result.isError)).toBe(variant !== "empty");
    if (variant === "nonzero") expect(result.content[0]!.text).toContain("exit code 2");
    if (variant === "fault") expect(result.content[0]!.text).toContain(record.execution_error);
    if (variant === "empty_fault") {
      expect(result.details).toMatchObject({ error: "unspecified execution failure" });
      expect(result.content[0]!.text).toContain("unspecified execution failure");
    }
    if (variant === "unknown_exit") {
      expect(result.details).toMatchObject({ error: "exit code unavailable" });
      expect(result.content[0]!.text).toContain("exit code unavailable");
    }
    if (variant === "missing" || variant === "unreadable") expect(result.content[0]!.text).toContain(`Log ${variant}`);
    const file = JSON.parse(await readFile(recordsPath, "utf8")) as BashRecordsFile;
    expect(file.records[record.bash_id]!.notification_consumed_at_ms != null).toBe(variant === "empty" || variant === "nonzero");
  } finally {
    await shutdown?.();
    vi.unstubAllEnvs();
    await rm(root, { recursive: true, force: true });
  }
  },
);

it("inspects /ps:logs after cold recovery without acknowledging the result", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "bash-cold-log-inspection-"));
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", "p9001");
  const dir = path.join(root, "pi-bash", "p9001");
  const log = path.join(root, "cold.log");
  await mkdir(dir, { recursive: true });
  await writeFile(log, "cold output");
  const record: BashRecord = {
    bash_id: "b-00000002", command: "cold fixture", cwd: root, pid: null, status: "exited", exit_code: 0,
    is_background: true, is_tracked: true, started_at_ms: 1, ended_at_ms: 2, log_path: log,
    stdout_log_path: log, stderr_log_path: log, log_bytes: 11, timeout_min: 55,
  };
  const recordsPath = path.join(dir, "bash-records.json");
  await writeFile(recordsPath, JSON.stringify({ v: 1, spawn_id: "p9001", updated_at_ms: 1, records: { [record.bash_id]: record } }));
  const commands = new Map<string, { handler: (args: string, ctx: ExtensionCommandContext) => Promise<void> }>();
  let shutdown: (() => Promise<void>) | undefined;
  extension({
    registerTool: () => {}, registerCommand: (name: string, command: { handler: (args: string, ctx: ExtensionCommandContext) => Promise<void> }) => commands.set(name, command),
    on: (name: string, callback: (event: { reason: string }) => Promise<void>) => { if (name === "session_shutdown") shutdown = () => callback({ reason: "quit" }); },
  } as unknown as ExtensionAPI);
  const notify = vi.fn();
  const ctx = { hasUI: true, mode: "rpc", ui: { notify, custom: async () => undefined } } as unknown as ExtensionCommandContext;
  try {
    await commands.get("ps:logs")!.handler(record.bash_id, ctx);
    expect(notify).toHaveBeenCalledWith("cold output", "info");
    const file = JSON.parse(await readFile(recordsPath, "utf8")) as BashRecordsFile;
    expect(file.records[record.bash_id]!.notification_consumed_at_ms).toBeUndefined();
  } finally {
    await shutdown?.();
    vi.unstubAllEnvs();
    await rm(root, { recursive: true, force: true });
  }
});
