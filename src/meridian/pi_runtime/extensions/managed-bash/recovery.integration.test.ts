import { readFileSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BashRecord, BashRecordsFile } from "../shared/schemas";
import { BashRuntime, type BashListRow } from "./src/bash_runtime";
import type { ShellTask } from "./src/shell_task";
import { until } from "../shared/test_wait";

// Real private files, with two gates at the filesystem boundary: every read of the records file is
// counted (and, when armed, held after it has read its bytes, so a held read stays stale), and
// records renames can be made to fail.
const gate = vi.hoisted(() => ({
  holdReads: false, failRename: false, reads: 0, inflight: 0, maxInflight: 0,
  held: [] as Array<() => void>,
}));

vi.mock("node:fs/promises", async (importOriginal) => {
  const fs = await importOriginal<typeof import("node:fs/promises")>();
  const isRecords = (file: unknown): boolean => String(file).endsWith("bash-records.json");
  return {
    ...fs,
    readFile: async (file: Parameters<typeof fs.readFile>[0], ...rest: unknown[]) => {
      const read = fs.readFile as (...args: unknown[]) => Promise<unknown>;
      if (!isRecords(file)) return read(file, ...rest);
      gate.reads += 1;
      gate.inflight += 1;
      gate.maxInflight = Math.max(gate.maxInflight, gate.inflight);
      try {
        const bytes = await read(file, ...rest);
        if (gate.holdReads) await new Promise<void>((release) => gate.held.push(release));
        return bytes;
      } finally { gate.inflight -= 1; }
    },
    rename: async (from: string, to: string) => {
      if (gate.failRename && isRecords(to)) throw Object.assign(new Error("EIO: injected records write failure"), { code: "EIO" });
      return fs.rename(from, to);
    },
  };
});

const SPAWN = "p-recovery";
const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
const stored = (bashId: string, patch: Partial<BashRecord> = {}): BashRecord => ({
  bash_id: bashId, command: "sleep 30", cwd: "/", pid: null, status: "running", is_background: true, is_tracked: true,
  exit_code: null, started_at_ms: 1, ended_at_ms: null, log_path: "l", stdout_log_path: "o", stderr_log_path: "e",
  log_bytes: 3, timeout_min: 1, ...patch,
});

type Fixture = ReturnType<typeof fixture>;

function fixture(root: string) {
  const dir = path.join(root, "pi-bash", SPAWN);
  const file = path.join(dir, "bash-records.json");
  const requests: Array<Promise<unknown>> = [];
  const exitFlag = path.join(root, "exit");
  return {
    file, exitFlag,
    command: `until [ -e '${exitFlag}' ]; do sleep 0.01; done; printf done`,
    /** Track a request so teardown awaits it even when an assertion has already failed. */
    request<T>(promise: Promise<T>): Promise<T> { requests.push(promise.catch(() => undefined)); return promise; },
    async seed(...rows: BashRecord[]): Promise<void> {
      await mkdir(dir, { recursive: true });
      const records = Object.fromEntries(rows.map((row) => [row.bash_id, row]));
      await writeFile(file, JSON.stringify({ v: 1, spawn_id: SPAWN, updated_at_ms: 1, records }));
    },
    async seedMalformed(): Promise<void> {
      await mkdir(dir, { recursive: true });
      await writeFile(file, JSON.stringify({ v: 1, spawn_id: "p-someone-else", updated_at_ms: 1, records: {} }));
    },
    bytes: (): string => readFileSync(file, "utf8"),
    disk: (id: string): BashRecord | undefined => (JSON.parse(readFileSync(file, "utf8")) as BashRecordsFile).records[id],
    async drain(): Promise<void> {
      gate.holdReads = false;
      for (const release of gate.held.splice(0)) release();
      await Promise.allSettled(requests);
    },
  };
}

async function withRuntime(body: (f: Fixture, open: () => BashRuntime) => Promise<void>): Promise<void> {
  const root = await mkdtemp(path.join(tmpdir(), "pi-recovery-"));
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", SPAWN);
  Object.assign(gate, { holdReads: false, failRename: false, reads: 0, inflight: 0, maxInflight: 0 });
  gate.held.length = 0;
  const f = fixture(root);
  let runtime: BashRuntime | undefined;
  try { await body(f, () => runtime = new BashRuntime()); }
  finally {
    gate.failRename = false;
    await writeFile(f.exitFlag, ""); // cooperative release, even when an assertion failed
    await f.drain();
    // Use the owned task's fault/repair path, not a recorded numeric PID. It also settles
    // waiters if a deliberately broken recovery control dropped their published row.
    const live = runtime as unknown as { executions: Map<string, { task: ShellTask | null }> } | undefined;
    for (const exec of live?.executions.values() ?? []) {
      exec.task?.child.stdout?.emit("error", new Error("recovery fixture cleanup"));
    }
    await runtime?.shutdown().catch(() => undefined);
    await rm(root, { recursive: true, force: true });
  }
}

afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); });

const listing = (runtime: BashRuntime) => runtime.manage({ action: "list", include_completed: true }) as Promise<{ rows: BashListRow[] }>;

describe("managed Bash recovery single flight", () => {
  it("concurrent commands after a failed recovery share one retry, and a stale read cannot overwrite a new task's row", async () => {
    await withRuntime(async (f, open) => {
      await f.seedMalformed();
      const runtime = open(); // the cold-start recovery fails closed
      await until(() => gate.reads === 1 && gate.inflight === 0);
      await f.seed(stored("b-cafe0001"));
      gate.holdReads = true;

      const starting = f.request(runtime.execute({ command: f.command, background: true }, undefined));
      const listings = [f.request(listing(runtime)), f.request(listing(runtime))];
      await until(() => gate.held.length >= 1);
      await pause(50); // any second recovery would have read its (soon stale) bytes by now
      const observed = { reads: gate.reads, maxInflight: gate.maxInflight, held: gate.held.length };

      // Let the first recovery finish and the task start, then release whatever else was held.
      gate.held.shift()?.();
      const id = ((await starting) as { bash_id: string }).bash_id;
      await f.drain();
      await Promise.all(listings);
      await f.drain();

      await writeFile(f.exitFlag, "");
      await until(() => runtime.list(true).find((row) => row.bash_id === id)?.status === "exited");
      await until(() => f.disk(id)?.status === "exited");

      expect(observed).toEqual({ reads: 2, maxInflight: 1, held: 1 });
      expect(f.disk("b-cafe0001")?.execution_error?.match(/ownership_lost/g)).toHaveLength(1);
      expect(Object.keys((JSON.parse(f.bytes()) as BashRecordsFile).records).sort()).toEqual([id, "b-cafe0001"].sort());
    });
  });

  it("malformed evidence fails closed for every caller; each command retries once; a repaired store recovers", async () => {
    await withRuntime(async (f, open) => {
      await f.seedMalformed();
      const runtime = open();
      await until(() => gate.reads === 1 && gate.inflight === 0);
      const evidence = f.bytes();

      const refusals = await Promise.allSettled([1, 2, 3].map(() => f.request(listing(runtime))));
      expect(refusals.map((r) => r.status)).toEqual(["rejected", "rejected", "rejected"]);
      for (const r of refusals) expect(String((r as PromiseRejectedResult).reason)).toContain("Invalid managed Bash record store");
      expect(gate.reads).toBe(2); // the cold start plus one shared retry
      expect(gate.maxInflight).toBe(1);

      await expect(f.request(listing(runtime))).rejects.toThrow("Invalid managed Bash record store");
      expect(gate.reads).toBe(3); // a later command retries once
      expect(f.bytes()).toBe(evidence); // never replaced
      expect(runtime.list(true)).toEqual([]);

      await f.seed(stored("b-cafe0002"));
      const { rows } = await f.request(listing(runtime));
      expect(rows).toMatchObject([{ bash_id: "b-cafe0002", status: "running" }]);
      expect(gate.reads).toBe(4);
    });
  });

  it("a failed recovery write adopts nothing and is shared; cold ownership loss never signals the stored PID", async () => {
    await withRuntime(async (f, open) => {
      const signals = vi.spyOn(process, "kill").mockImplementation(() => true); // records any signal, sends none
      await f.seed(stored("b-cafe0003", { pid: process.pid }));
      const seeded = f.bytes();
      gate.failRename = true;
      const runtime = open();
      await until(() => gate.reads === 1 && gate.inflight === 0);

      const failures = await Promise.allSettled([1, 2, 3].map(() => f.request(listing(runtime))));
      for (const r of failures) {
        expect(r.status).toBe("rejected");
        expect(String((r as PromiseRejectedResult).reason)).toContain("EIO");
      }
      expect(gate.reads).toBe(2);
      expect(gate.maxInflight).toBe(1);
      expect(runtime.list(true)).toEqual([]);
      expect(f.bytes()).toBe(seeded);

      gate.failRename = false;
      const { rows } = await f.request(listing(runtime));
      expect(rows[0]).toMatchObject({ bash_id: "b-cafe0003", status: "running", pid: process.pid });
      expect(rows[0]!.execution_error).toContain("ownership_lost");
      expect(await runtime.manage({ action: "kill", bash_id: "b-cafe0003" })).toMatchObject({ killed: false, message: expect.stringContaining("no PID was signalled") });
      expect(await runtime.manage({ action: "wait", bash_id: "b-cafe0003" })).toMatchObject({ error: expect.stringContaining("ownership_lost") });
      expect(await runtime.manage({ action: "detach", bash_id: "b-cafe0003" })).toMatchObject({ detached: true });
      expect(signals).not.toHaveBeenCalled();
    });
  });
});
