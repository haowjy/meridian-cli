import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { SpawnWatchRuntime } from "./index";
import { rememberSpawnOriginBashIds } from "../../shared/spawn_origins";
import type { BashRecord, BashRecordsFile } from "../../shared/schemas";

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

type SpawnWatchRuntimeInternals = SpawnWatchRuntime & {
  scanBashRecords(): Promise<void>;
  scanSpawns(): Promise<void>;
  fallbackScanReasons: Set<string>;
  pending: Map<string, { kind: "spawn" | "bash"; duration: string }>;
  running: boolean;
  enableDiscoveryPolling(): void;
  stopDiscoveryPolling(): void;
  slowDiscoveryPolling(): void;
  fallbackScanInterval: NodeJS.Timeout | null;
  discoveryScanInterval: NodeJS.Timeout | null;
  missingStateFirstSeenMs: Map<string, number>;
};

function bashRecord(bashId: string, overrides: Partial<BashRecord> = {}): BashRecord {
  return {
    bash_id: bashId,
    command: `meridian spawn -m test ${bashId}`,
    cwd: "/tmp",
    pid: null,
    status: "exited",
    is_background: true,
    is_tracked: true,
    exit_code: 0,
    started_at_ms: Date.now() - 10_000,
    ended_at_ms: Date.now() - 5_000,
    log_path: "/tmp/log",
    stdout_log_path: "/tmp/stdout",
    stderr_log_path: "/tmp/stderr",
    log_bytes: 0,
    timeout_min: 55,
    originating_bash_id: null,
    ...overrides,
  };
}

async function writeBashRecords(runtimeRoot: string, spawnId: string, records: BashRecord[]): Promise<void> {
  const bashDir = path.join(runtimeRoot, "pi-bash", spawnId);
  await mkdir(bashDir, { recursive: true });
  const file: BashRecordsFile = {
    v: 1,
    spawn_id: spawnId,
    updated_at_ms: Date.now(),
    records: Object.fromEntries(records.map((record) => [record.bash_id, record])),
  };
  await writeFile(path.join(bashDir, "bash-records.json"), JSON.stringify(file));
}

async function bashRecordWithSpawnOutput(
  runtimeRoot: string,
  bashId: string,
  spawnId: string,
  overrides: Partial<BashRecord> = {},
): Promise<BashRecord> {
  const logPath = path.join(runtimeRoot, "logs", `${bashId}.log`);
  await mkdir(path.dirname(logPath), { recursive: true });
  await writeFile(logPath, `Spawn id: ${spawnId}\n`);
  return bashRecord(bashId, {
    log_path: logPath,
    stdout_log_path: logPath,
    stderr_log_path: logPath,
    log_bytes: `Spawn id: ${spawnId}\n`.length,
    ...overrides,
  });
}

async function writeSpawnState(
  runtimeRoot: string,
  spawnId: string,
  values: {
    parentId?: string | null;
    originBashId?: string | null;
    status?: string;
    terminal?: Record<string, unknown> | null;
  } = {},
): Promise<void> {
  const spawnDir = path.join(runtimeRoot, "spawns", spawnId);
  await mkdir(spawnDir, { recursive: true });
  await writeFile(
    path.join(spawnDir, "state.json"),
    JSON.stringify({
      id: spawnId,
      parent_id: values.parentId ?? null,
      originating_bash_id: values.originBashId ?? null,
      status: values.status ?? "running",
      terminal: values.terminal ?? null,
    }),
  );
}

function terminalFacts(status = "succeeded"): Record<string, unknown> {
  return {
    // Terminal status lives only at the top level of state.json.
    exit_code: status === "succeeded" ? 0 : 1,
    finished_at: "2026-07-17T12:00:05Z",
    published_at: "2026-07-17T12:00:05Z",
    duration_secs: 65,
  };
}

async function makeRuntime(): Promise<{ runtimeRoot: string; runtime: SpawnWatchRuntime; internals: SpawnWatchRuntimeInternals }> {
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "spawn-watch-origin-"));
  setEnv("_MERIDIAN_PI_STATE_DIR", runtimeRoot);
  setEnv("MERIDIAN_SPAWN_ID", "p-parent");
  const runtime = new SpawnWatchRuntime({} as ConstructorParameters<typeof SpawnWatchRuntime>[0]);
  return { runtimeRoot, runtime, internals: runtime as SpawnWatchRuntimeInternals };
}

describe("SpawnWatchRuntime bash-origin spawn tracking", () => {
  afterEach(() => restoreEnv());

  it("lists only spawns launched by this session's managed bash commands", async () => {
    const { runtimeRoot, runtime } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [bashRecord("b-origin")]);
      await writeSpawnState(runtimeRoot, "p1001", { originBashId: "b-origin", status: "running" });
      await writeSpawnState(runtimeRoot, "p-parent-child", { parentId: "p-parent", status: "running" });
      await writeSpawnState(runtimeRoot, "p-other-origin", { originBashId: "b-other", status: "running" });

      expect((await runtime.rows()).map((state) => state.id)).toEqual(["p1001"]);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });


  it("ignores parent-only spawns when managed-bash origins are absent", async () => {
    const { runtimeRoot, runtime } = await makeRuntime();
    try {
      await writeSpawnState(runtimeRoot, "p-parent-child", { parentId: "p-parent", status: "running" });

      expect(await runtime.rows()).toEqual([]);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("polls while bash-origin spawns are running and stops when they finish", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [bashRecord("b-origin")]);
      await writeSpawnState(runtimeRoot, "p1001", { originBashId: "b-origin", status: "running" });
      internals.running = true;

      await internals.scanSpawns();
      expect(internals.fallbackScanReasons.has("active-origin-spawns")).toBe(true);

      await writeSpawnState(runtimeRoot, "p1001", {
        originBashId: "b-origin",
        status: "succeeded",
        terminal: terminalFacts(),
      });
      await internals.scanSpawns();
      expect(internals.fallbackScanReasons.has("active-origin-spawns")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("does not report a status-only row as completed", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [bashRecord("b-status-only")]);
      await writeSpawnState(runtimeRoot, "p-status-only", {
        originBashId: "b-status-only",
        status: "succeeded",
        terminal: null,
      });
      internals.running = true;

      await internals.scanSpawns();

      expect(internals.pending.has("p-status-only")).toBe(false);
      expect(internals.fallbackScanReasons.has("active-origin-spawns")).toBe(true);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("reads terminal duration from nested persisted facts", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [bashRecord("b-duration")]);
      await writeSpawnState(runtimeRoot, "p-duration", {
        originBashId: "b-duration",
        status: "succeeded",
        terminal: {
          status: "succeeded",
          exit_code: 0,
          finished_at: "2026-07-17T12:00:05Z",
          published_at: "2026-07-17T12:00:05Z",
          duration_secs: 65,
        },
      });

      await internals.scanSpawns();

      expect(internals.pending.get("p-duration")?.duration).toBe("1m05s");
      expect(internals.pending.get("p-duration")?.kind).toBe("spawn");
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });


  it("keeps polling when a spawn directory appears before state.json", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [
        await bashRecordWithSpawnOutput(runtimeRoot, "b-origin", "p1001"),
      ]);
      await mkdir(path.join(runtimeRoot, "spawns", "p1001"), { recursive: true });
      internals.running = true;

      await internals.scanBashRecords();
      expect(internals.fallbackScanReasons.has("spawn-discovery")).toBe(true);
      expect(internals.fallbackScanReasons.has("active-origin-spawns")).toBe(false);

      await writeSpawnState(runtimeRoot, "p1001", { originBashId: "b-origin", status: "running" });
      await internals.scanSpawns();
      expect(internals.fallbackScanReasons.has("active-origin-spawns")).toBe(true);
      expect(internals.fallbackScanInterval).not.toBeNull();

      internals.stopDiscoveryPolling();
      expect(internals.fallbackScanReasons.has("active-origin-spawns")).toBe(true);
      expect(internals.fallbackScanInterval).not.toBeNull();
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("keeps slow discovery polling until missing state.json files resolve", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [
        await bashRecordWithSpawnOutput(runtimeRoot, "b-origin", "p1001"),
      ]);
      await mkdir(path.join(runtimeRoot, "spawns", "p1001"), { recursive: true });
      internals.running = true;

      await internals.scanBashRecords();
      internals.slowDiscoveryPolling();
      expect(internals.fallbackScanReasons.has("spawn-discovery")).toBe(true);
      expect(internals.discoveryScanInterval).toBeNull();
      expect(internals.fallbackScanInterval).not.toBeNull();

      await writeSpawnState(runtimeRoot, "p1001", {
        originBashId: "b-origin",
        status: "succeeded",
        terminal: terminalFacts(),
      });
      await internals.scanSpawns();
      expect(internals.fallbackScanReasons.has("spawn-discovery")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("keeps correlated spawns visible after finished bash records are cleared", async () => {
    const { runtimeRoot, runtime } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [bashRecord("b-origin")]);
      await writeSpawnState(runtimeRoot, "p1001", { originBashId: "b-origin", status: "running" });

      expect((await runtime.rows()).map((state) => state.id)).toEqual(["p1001"]);

      await writeBashRecords(runtimeRoot, "p-parent", []);
      expect((await runtime.rows()).map((state) => state.id)).toEqual(["p1001"]);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("picks up origin sidecar ids written after the first scan", async () => {
    const { runtimeRoot, runtime } = await makeRuntime();
    try {
      await writeSpawnState(runtimeRoot, "p2001", { originBashId: "b-late", status: "running" });

      expect(await runtime.rows()).toEqual([]);

      await rememberSpawnOriginBashIds(["b-late"], "p-parent");
      expect((await runtime.rows()).map((state) => state.id)).toEqual(["p2001"]);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });


  it("withholds bash completion while discovery polling may still find a spawn", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [
        await bashRecordWithSpawnOutput(runtimeRoot, "b-origin", "p1001"),
      ]);
      await mkdir(path.join(runtimeRoot, "spawns", "p1001"), { recursive: true });
      internals.running = true;

      await internals.scanBashRecords();
      expect(internals.pending.has("b-timeout")).toBe(false);

      await writeSpawnState(runtimeRoot, "p1001", { originBashId: "b-origin", status: "running" });
      await internals.scanBashRecords();
      expect(internals.pending.has("b-timeout")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("releases bash completion when expected spawn state never appears", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [
        await bashRecordWithSpawnOutput(runtimeRoot, "b-timeout", "p1003"),
      ]);
      internals.running = true;

      await internals.scanBashRecords();
      expect(internals.pending.has("b-timeout")).toBe(false);

      internals.missingStateFirstSeenMs.set("p1003", Date.now() - 16_000);
      internals.slowDiscoveryPolling();
      await internals.scanBashRecords();
      expect(internals.pending.get("b-timeout")?.kind).toBe("bash");
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });


  it("manual refresh ignores unowned empty spawn dirs", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await mkdir(path.join(runtimeRoot, "spawns", "p-late"), { recursive: true });
      internals.running = true;

      await runtime.rows(true);
      expect(internals.fallbackScanReasons.has("spawn-discovery")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("starts discovery polling for expected spawn ids without state.json", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "spawn-watch-origin-start-"));
    setEnv("_MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-parent");
    await writeBashRecords(runtimeRoot, "p-parent", [
      await bashRecordWithSpawnOutput(runtimeRoot, "b-origin", "p1002"),
    ]);

    const runtime = new SpawnWatchRuntime({} as ConstructorParameters<typeof SpawnWatchRuntime>[0]);
    const internals = runtime as SpawnWatchRuntimeInternals;

    try {
      runtime.start();
      await internals.scanBashRecords();
      expect(internals.fallbackScanReasons.has("spawn-discovery")).toBe(true);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });


  it("does not let stale discovery suppress old bash completions", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [
        bashRecord("b-old", { ended_at_ms: Date.now() - 20_000 }),
      ]);
      await mkdir(path.join(runtimeRoot, "spawns", "p-orphan"), { recursive: true });
      internals.running = true;
      internals.enableDiscoveryPolling();

      await internals.scanBashRecords();
      expect(internals.pending.get("b-old")?.kind).toBe("bash");
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });


  it("does not let global discovery suppress non-spawn bash completions", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [
        await bashRecordWithSpawnOutput(runtimeRoot, "b-shell", "p9999", {
          command: "echo 'Spawn id: p9999'",
          ended_at_ms: Date.now() - 5_000,
        }),
      ]);
      await mkdir(path.join(runtimeRoot, "spawns", "p-orphan"), { recursive: true });
      internals.running = true;
      internals.enableDiscoveryPolling();

      await internals.scanBashRecords();
      expect(internals.pending.get("b-shell")?.kind).toBe("bash");
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("suppresses bash completion when the bash command launched a spawn", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [bashRecord("b-origin")]);
      await writeSpawnState(runtimeRoot, "p1001", { originBashId: "b-origin", status: "running" });

      await internals.scanBashRecords();
      expect(internals.pending.has("b-timeout")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });
});
