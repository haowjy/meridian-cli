import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import type { BashRecordsFile } from "../shared/schemas";
import { SpawnWatchRuntime } from "../meridian-spawn-watch/src/index";
import managedBashExtension from "./src/index";

const savedEnv: Record<string, string | undefined> = {};

function setEnv(key: string, value: string): void {
  if (!(key in savedEnv)) savedEnv[key] = process.env[key];
  process.env[key] = value;
}

function restoreEnv(): void {
  for (const [key, value] of Object.entries(savedEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
    delete savedEnv[key];
  }
}

async function waitForTerminalRecord(recordsPath: string, bashId: string): Promise<BashRecordsFile> {
  const deadline = Date.now() + 1_000;
  while (Date.now() < deadline) {
    const file = JSON.parse(await readFile(recordsPath, "utf-8")) as BashRecordsFile;
    if (file.records[bashId]?.status !== "running") return file;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`bash record did not become terminal: ${bashId}`);
}

type RegisteredTool = {
  execute: (...args: unknown[]) => Promise<{ details?: unknown }>;
};

async function waitForMessages(messages: unknown[]): Promise<void> {
  const deadline = Date.now() + 2_000;
  while (Date.now() < deadline) {
    if (messages.length) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw Error("terminal Bash result was not delivered");
}

function makeManagedBashHost(): {
  tools: Map<string, RegisteredTool>;
  shutdown(): Promise<void>;
} {
  const tools = new Map<string, RegisteredTool>();
  let shutdownHook: (() => void | Promise<void>) | null = null;
  const pi = {
    registerTool: (definition: { name: string; execute: RegisteredTool["execute"] }) => tools.set(definition.name, definition),
    registerCommand: () => undefined,
    on: (event: string, handler: () => void | Promise<void>) => {
      if (event === "session_shutdown") shutdownHook = handler;
    },
  };
  managedBashExtension(pi as unknown as Parameters<typeof managedBashExtension>[0]);
  return {
    tools,
    shutdown: async () => {
      await shutdownHook?.();
    },
  };
}

describe("managed bash and spawn-watch completion notifications", () => {
  afterEach(() => restoreEnv());

  it("suppresses waited completion and sends unattended completion once", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "pi-bash-notification-dedup-"));
    setEnv("_MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-notification-dedup");

    const host = makeManagedBashHost();
    const { tools } = host;

    const messages: unknown[] = [];
    let idle = false;
    const spawnWatch = new SpawnWatchRuntime({
      sendMessage: (message: unknown) => messages.push(message),
    } as unknown as ConstructorParameters<typeof SpawnWatchRuntime>[0], () => idle);
    spawnWatch.start();

    try {
      const bashTool = tools.get("bash");
      const manageTool = tools.get("bash_manage");
      expect(bashTool).toBeDefined();
      expect(manageTool).toBeDefined();

      const started = await bashTool!.execute("call", {
        command: "sleep 0.02; printf done",
        background: true,
      });
      const bashId = (started.details as { bash_id: string }).bash_id;
      const waited = await manageTool!.execute("call", { action: "wait", bash_id: bashId });
      expect((waited.details as { status: string }).status).toBe("exited");

      const recordsPath = path.join(runtimeRoot, "pi-bash", "p-notification-dedup", "bash-records.json");
      const records = await waitForTerminalRecord(recordsPath, bashId);
      expect(typeof records.records[bashId]?.notification_consumed_at_ms).toBe("number");
      idle = true;
      spawnWatch.observeIdle(() => idle);
      await new Promise((resolve) => setTimeout(resolve, 400));
      expect(messages).toHaveLength(0);

      const unattended = await bashTool!.execute("call", {
        command: "sleep 0.02; printf unattended",
        background: true,
      });
      const unattendedBashId = (unattended.details as { bash_id: string }).bash_id;
      await waitForTerminalRecord(recordsPath, unattendedBashId);
      await waitForMessages(messages);
      await spawnWatch.admitMessage({role: "custom", ...(messages[0] as object)});
      await new Promise((resolve) => setTimeout(resolve, 600));

      expect(messages).toHaveLength(1);
      expect((messages[0] as { content: string }).content).toContain(unattendedBashId);
      expect((messages[0] as { content: string }).content).not.toContain(bashId);
    } finally {
      await host.shutdown();
      spawnWatch.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("leaves a timed-out running wait eligible for later completion notification", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "pi-bash-wait-timeout-"));
    setEnv("_MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-notification-timeout");

    const host = makeManagedBashHost();
    const { tools } = host;
    const messages: unknown[] = [];
    const spawnWatch = new SpawnWatchRuntime({
      sendMessage: (message: unknown) => messages.push(message),
    } as unknown as ConstructorParameters<typeof SpawnWatchRuntime>[0], () => true);
    spawnWatch.start();

    try {
      const bashId = ((await tools.get("bash")!.execute("call", {
        command: "sleep 30",
        background: true,
      })).details as { bash_id: string }).bash_id;
      const recordsPath = path.join(runtimeRoot, "pi-bash", "p-notification-timeout", "bash-records.json");

      vi.useFakeTimers();
      const wait = tools.get("bash_manage")!.execute("call", {
        action: "wait",
        bash_id: bashId,
      });
      await vi.advanceTimersByTimeAsync(55 * 60_000);
      expect(((await wait).details as { status: string }).status).toBe("running");
      const records = JSON.parse(await readFile(recordsPath, "utf-8")) as BashRecordsFile;
      expect(records.records[bashId]?.notification_consumed_at_ms).toBeUndefined();
      vi.useRealTimers();

      const killed = await tools.get("bash_manage")!.execute("call", { action: "kill", bash_id: bashId });
      expect((killed.details as { killed: boolean }).killed).toBe(true);
      await waitForTerminalRecord(recordsPath, bashId);
      await waitForMessages(messages);
      expect(messages).toHaveLength(1);
      expect((messages[0] as { content: string }).content).toContain(bashId);
    } finally {
      vi.useRealTimers();
      await host.shutdown();
      spawnWatch.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("does not retain wait consumption when its record write fails", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "pi-bash-wait-write-failure-"));
    setEnv("_MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-notification-write-failure");

    const host = makeManagedBashHost();
    const { tools } = host;

    try {
      const bashTool = tools.get("bash")!;
      const manageTool = tools.get("bash_manage")!;
      const started = await bashTool.execute("call", {
        command: "sleep 0.02",
        background: true,
      });
      const bashId = (started.details as { bash_id: string }).bash_id;
      const recordsPath = path.join(runtimeRoot, "pi-bash", "p-notification-write-failure", "bash-records.json");
      await waitForTerminalRecord(recordsPath, bashId);

      const previous = await readFile(recordsPath, "utf-8");
      await rm(recordsPath);
      await mkdir(recordsPath);
      try {
        await expect(manageTool.execute("call", { action: "wait", bash_id: bashId })).rejects.toThrow();
      } finally {
        await rm(recordsPath, { recursive: true });
        await writeFile(recordsPath, previous);
      }
      const later = await bashTool.execute("call", { command: "true", background: true });
      const laterBashId = (later.details as { bash_id: string }).bash_id;
      await waitForTerminalRecord(recordsPath, laterBashId);
      const records = JSON.parse(await readFile(recordsPath, "utf-8")) as BashRecordsFile;
      expect(records.records[bashId]?.notification_consumed_at_ms).toBeUndefined();
    } finally {
      await host.shutdown();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });
});
