import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { SpawnWatchRuntime } from "./index";
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
  pending: Map<string, { kind: "spawn" | "bash" }>;
  running: boolean;
  enableDiscoveryPolling(): void;
  stopDiscoveryPolling(): void;
  fallbackScanInterval: NodeJS.Timeout | null;
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

async function writeSpawnState(
  runtimeRoot: string,
  spawnId: string,
  values: { parentId?: string | null; originBashId?: string | null; status?: string } = {},
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
    }),
  );
}

async function makeRuntime(): Promise<{ runtimeRoot: string; runtime: SpawnWatchRuntime; internals: SpawnWatchRuntimeInternals }> {
  const runtimeRoot = await mkdtemp(path.join(tmpdir(), "spawn-watch-origin-"));
  setEnv("MERIDIAN_PI_STATE_DIR", runtimeRoot);
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
      await writeSpawnState(runtimeRoot, "p-origin", { originBashId: "b-origin", status: "running" });
      await writeSpawnState(runtimeRoot, "p-parent-child", { parentId: "p-parent", status: "running" });
      await writeSpawnState(runtimeRoot, "p-other-origin", { originBashId: "b-other", status: "running" });

      expect((await runtime.rows()).map((state) => state.id)).toEqual(["p-origin"]);
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
      await writeSpawnState(runtimeRoot, "p-origin", { originBashId: "b-origin", status: "running" });
      internals.running = true;

      await internals.scanSpawns();
      expect(internals.fallbackScanReasons.has("active-origin-spawns")).toBe(true);

      await writeSpawnState(runtimeRoot, "p-origin", { originBashId: "b-origin", status: "succeeded" });
      await internals.scanSpawns();
      expect(internals.fallbackScanReasons.has("active-origin-spawns")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });


  it("keeps polling when a spawn directory appears before state.json", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [bashRecord("b-origin")]);
      await mkdir(path.join(runtimeRoot, "spawns", "p-origin"), { recursive: true });
      internals.running = true;

      internals.enableDiscoveryPolling();
      await internals.scanSpawns();
      expect(internals.fallbackScanReasons.has("spawn-discovery")).toBe(true);
      expect(internals.fallbackScanReasons.has("active-origin-spawns")).toBe(false);

      await writeSpawnState(runtimeRoot, "p-origin", { originBashId: "b-origin", status: "running" });
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

  it("keeps correlated spawns visible after finished bash records are cleared", async () => {
    const { runtimeRoot, runtime } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [bashRecord("b-origin")]);
      await writeSpawnState(runtimeRoot, "p-origin", { originBashId: "b-origin", status: "running" });

      expect((await runtime.rows()).map((state) => state.id)).toEqual(["p-origin"]);

      await writeBashRecords(runtimeRoot, "p-parent", []);
      expect((await runtime.rows()).map((state) => state.id)).toEqual(["p-origin"]);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });


  it("withholds bash completion while discovery polling may still find a spawn", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await writeBashRecords(runtimeRoot, "p-parent", [bashRecord("b-origin")]);
      await mkdir(path.join(runtimeRoot, "spawns", "p-origin"), { recursive: true });
      internals.running = true;
      internals.enableDiscoveryPolling();

      await internals.scanBashRecords();
      expect(internals.pending.has("b-origin")).toBe(false);

      await writeSpawnState(runtimeRoot, "p-origin", { originBashId: "b-origin", status: "running" });
      await internals.scanBashRecords();
      expect(internals.pending.has("b-origin")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });


  it("manual refresh starts discovery polling for missed empty spawn dirs", async () => {
    const { runtimeRoot, runtime, internals } = await makeRuntime();
    try {
      await mkdir(path.join(runtimeRoot, "spawns", "p-late"), { recursive: true });
      internals.running = true;

      await runtime.rows(true);
      expect(internals.fallbackScanReasons.has("spawn-discovery")).toBe(true);

      internals.stopDiscoveryPolling();
      expect(internals.fallbackScanReasons.has("spawn-discovery")).toBe(false);

      await runtime.rows(true);
      expect(internals.fallbackScanReasons.has("spawn-discovery")).toBe(true);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });

  it("starts discovery polling for existing spawn dirs without state.json", async () => {
    const runtimeRoot = await mkdtemp(path.join(tmpdir(), "spawn-watch-origin-start-"));
    setEnv("MERIDIAN_PI_STATE_DIR", runtimeRoot);
    setEnv("MERIDIAN_SPAWN_ID", "p-parent");
    await mkdir(path.join(runtimeRoot, "spawns", "p-late"), { recursive: true });

    const runtime = new SpawnWatchRuntime({} as ConstructorParameters<typeof SpawnWatchRuntime>[0]);
    const internals = runtime as SpawnWatchRuntimeInternals;

    try {
      runtime.start();
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
        bashRecord("b-shell", { command: "sleep 1", ended_at_ms: Date.now() - 5_000 }),
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
      await writeSpawnState(runtimeRoot, "p-origin", { originBashId: "b-origin", status: "running" });

      await internals.scanBashRecords();
      expect(internals.pending.has("b-origin")).toBe(false);
    } finally {
      runtime.stop();
      await rm(runtimeRoot, { recursive: true, force: true });
    }
  });
});
