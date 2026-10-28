import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BashRecord, BashRecordsFile } from "../shared/schemas";
import { BashRuntime } from "./src/bash_runtime";
import { until } from "../shared/test_wait";

type Verdict = "ok" | "fail";
type Held = { when: (file: BashRecordsFile) => boolean; hit: (file: BashRecordsFile) => void; verdict: Promise<Verdict> };

// Records-file renames are held at the durable boundary by payload predicate (FIFO), and can be made to
// fail for every attempt. Every attempted payload is logged, so "no write" is observable.
const gate = vi.hoisted(() => ({ holds: [] as Held[], failAlways: false, log: [] as BashRecordsFile[] }));

vi.mock("node:fs/promises", async (importOriginal) => {
  const fs = await importOriginal<typeof import("node:fs/promises")>();
  const eio = (): Error => Object.assign(new Error("EIO: injected records write failure"), { code: "EIO" });
  return {
    ...fs,
    rename: async (from: string, to: string) => {
      if (String(to).endsWith("bash-records.json")) {
        const file = JSON.parse(await fs.readFile(from, "utf8")) as BashRecordsFile;
        gate.log.push(file);
        if (gate.failAlways) throw eio();
        if (gate.holds[0]?.when(file)) {
          const held = gate.holds.shift()!;
          held.hit(file);
          if (await held.verdict === "fail") throw eio();
        }
      }
      return fs.rename(from, to);
    },
  };
});

const SPAWN = "p-coordination";
const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
async function stillPending(promise: Promise<unknown>): Promise<boolean> {
  const marker = Symbol("pending");
  return (await Promise.race([promise.then(() => "settled", () => "settled"), pause(40).then(() => marker)])) === marker;
}

type Hold = { hit: Promise<BashRecordsFile>; settle: (verdict: Verdict) => void };
type Deferred = { promise: Promise<void>; resolve: () => void; reject: (error: Error) => void };

function deferred(): Deferred {
  const d = {} as Deferred;
  d.promise = new Promise<void>((resolve, reject) => { d.resolve = resolve; d.reject = reject; });
  d.promise.catch(() => undefined);
  return d;
}

type Harness = Awaited<ReturnType<typeof open>>;

/** One runtime on private files. Tasks wait for a flag file, so every exit is cooperative. */
async function open(root: string) {
  const recordsPath = path.join(root, "pi-bash", SPAWN, "bash-records.json");
  const flag = path.join(root, "exit");
  const requests: Array<Promise<unknown>> = [];
  const holds: Hold[] = [];
  const pings: Array<{ id: string; verdict: Deferred }> = [];
  const runtime = new BashRuntime({
    onBackgroundPing: (record) => { const verdict = deferred(); pings.push({ id: record.bash_id, verdict }); return verdict.promise; },
  });
  const h = {
    runtime, pings, command: `until [ -e '${flag}' ]; do sleep 0.01; done; printf done`,
    /** Track a request so teardown awaits it even if an assertion fails first. */
    request<T>(promise: Promise<T>): Promise<T> { requests.push(promise.catch(() => undefined)); return promise; },
    arm(when: Held["when"]): Hold {
      const hold = {} as Hold;
      hold.hit = new Promise<BashRecordsFile>((hit) => {
        const verdict = new Promise<Verdict>((resolve) => { hold.settle = resolve; });
        gate.holds.push({ when, hit, verdict });
      });
      holds.push(hold);
      return hold;
    },
    exit: (): Promise<void> => writeFile(flag, ""),
    row: (id: string) => runtime.list(true).find((row) => row.bash_id === id)!,
    disk: async (id: string): Promise<BashRecord> =>
      (JSON.parse(await readFile(recordsPath, "utf8")) as BashRecordsFile).records[id]!,
    async start(): Promise<string> {
      return ((await h.request(runtime.execute({ command: h.command, background: true }, undefined))) as { bash_id: string }).bash_id;
    },
    /** A queued no-op behind everything already enqueued: its slot runs after any pending release slot. */
    barrier: (): Promise<number> => runtime.clearFinished(),
    async teardown(): Promise<void> {
      gate.holds.length = 0; gate.failAlways = false;
      for (const hold of holds) hold.settle("ok");
      for (const ping of pings) ping.verdict.resolve();
      await h.exit().catch(() => undefined);
      await Promise.allSettled(requests);
      await runtime.shutdown().catch(() => undefined);
    },
  };
  return h;
}

async function withRuntime(body: (h: Harness) => Promise<void>): Promise<void> {
  const root = await mkdtemp(path.join(tmpdir(), "pi-coordination-"));
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", SPAWN);
  vi.stubEnv("_MERIDIAN_PI_TASK_PING_INTERVAL_MS", "20");
  gate.holds.length = 0; gate.failAlways = false; gate.log.length = 0;
  const h = await open(root);
  try { await body(h); }
  finally {
    await h.teardown();
    gate.log.length = 0;
    await rm(root, { recursive: true, force: true });
  }
}

afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); });

const wrote = (id: string, patch: Partial<BashRecord>): BashRecordsFile[] =>
  gate.log.filter((file) => Object.entries(patch).every(([key, value]) => (file.records[id] as Record<string, unknown> | undefined)?.[key] === value));

describe("managed Bash ping lifetime", () => {
  it.each(["terminal", "detach"] as const)("a notification failure finishing after %s releases nothing and invents no fault, even when storage is failing", async (ending) => {
    await withRuntime(async (h) => {
      const stderr = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
      const id = await h.start();
      await until(() => h.pings.length === 1); // the claim is published and its callback is held
      expect((await h.disk(id)).ping_sent_at_ms).toEqual(expect.any(Number));

      if (ending === "terminal") { await h.exit(); await until(() => h.row(id).status === "exited"); }
      else expect(await h.runtime.manage({ action: "detach", bash_id: id })).toMatchObject({ detached: true });
      const before = gate.log.length;
      gate.failAlways = true; // a release write that was still attempted would fail, and fail the task

      h.pings[0]!.verdict.reject(new Error("stale notification capability"));
      await until(() => stderr.mock.calls.some(([text]) => String(text).includes("stale notification capability")));
      await h.barrier(); // behind any release slot the failure queued
      gate.failAlways = false;

      expect(gate.log.slice(before)).toEqual([]); // no late write was even attempted
      expect(h.row(id).execution_error).toBeUndefined();
      expect(h.row(id).status).toBe(ending === "terminal" ? "exited" : "running");
      expect(await h.disk(id)).toMatchObject(ending === "terminal" ? { status: "exited" } : { status: "running", is_tracked: false });
      expect((await h.disk(id)).execution_error).toBeUndefined();
    });
  });

  it("an applicable release whose metadata write fails is still task-fatal", async () => {
    await withRuntime(async (h) => {
      vi.spyOn(process.stderr, "write").mockImplementation(() => true);
      const id = await h.start();
      await until(() => h.pings.length === 1);
      // Still running, tracked, background: the notification capability fails and its claim must be released.
      const release = h.arm((file) => file.records[id]?.ping_sent_at_ms === null);
      h.pings[0]!.verdict.reject(new Error("stale notification capability"));
      await release.hit;
      release.settle("fail");

      await until(() => h.row(id).status === "killed");
      expect(h.row(id).execution_error).toContain("EIO");
      await until(async () => (await h.disk(id)).status === "killed");
      expect(await h.disk(id)).toMatchObject({ status: "killed", execution_error: expect.stringContaining("EIO") });
    });
  });

  it("a capability failure alone is not a task failure: the running claim is released", async () => {
    await withRuntime(async (h) => {
      vi.spyOn(process.stderr, "write").mockImplementation(() => true);
      const id = await h.start();
      await until(() => h.pings.length === 1);
      h.pings[0]!.verdict.reject(new Error("stale notification capability"));
      await until(async () => (await h.disk(id)).ping_sent_at_ms === null);
      expect(h.row(id)).toMatchObject({ status: "running" });
      expect(h.row(id).execution_error).toBeUndefined();
    });
  });
});

describe("managed Bash detach result", () => {
  const creating = (h: Harness) => h.arm((file) => Object.keys(file.records).length > 0);

  it("a detach queued behind a creation that fails reports that nothing was detached", async () => {
    await withRuntime(async (h) => {
      const creation = creating(h);
      const starting = h.request(h.runtime.execute({ command: h.command, background: true }, undefined));
      await creation.hit;
      const id = h.runtime.list(true)[0]!.bash_id; // proposed, not published
      const detaching = h.request(h.runtime.manage({ action: "detach", bash_id: id }));
      expect(await stillPending(detaching)).toBe(true); // queued behind the held creation write

      creation.settle("fail");
      const result = await detaching;
      await expect(starting).rejects.toThrow("EIO");
      expect(result).toMatchObject({ kind: "detach", bash_id: id, detached: false });
      // The durable row is the failure repair, which was never released from tracking.
      await until(async () => (await h.disk(id)).status === "killed");
      expect(await h.disk(id)).toMatchObject({ is_tracked: true, execution_error: expect.stringContaining("EIO") });
      expect(wrote(id, { is_tracked: false })).toEqual([]);
    });
  });

  it("a detach queued behind a creation that lands is adopted and reported", async () => {
    await withRuntime(async (h) => {
      const creation = creating(h);
      const starting = h.request(h.runtime.execute({ command: h.command, background: true }, undefined));
      await creation.hit;
      const id = h.runtime.list(true)[0]!.bash_id;
      const detaching = h.request(h.runtime.manage({ action: "detach", bash_id: id }));
      expect(await stillPending(detaching)).toBe(true);

      creation.settle("ok");
      expect(await detaching).toMatchObject({ kind: "detach", bash_id: id, detached: true });
      await starting;
      expect(await h.disk(id)).toMatchObject({ is_tracked: false, status: "running" });
      expect(h.row(id)).toMatchObject({ is_tracked: false, status: "running" });
    });
  });

  it("a detach whose own write fails is still the caller's error and leaves tracking intact", async () => {
    await withRuntime(async (h) => {
      const id = await h.start();
      const write = h.arm((file) => file.records[id]?.is_tracked === false);
      const detaching = h.request(h.runtime.manage({ action: "detach", bash_id: id }));
      await write.hit;
      write.settle("fail");
      await expect(detaching).rejects.toThrow("EIO");
      expect(h.row(id)).toMatchObject({ status: "running", is_tracked: true });
      expect(h.row(id).execution_error).toBeUndefined();
      expect(await h.disk(id)).toMatchObject({ is_tracked: true });
    });
  });
});
