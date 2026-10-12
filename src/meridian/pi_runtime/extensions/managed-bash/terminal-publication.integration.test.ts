import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BashRecord, BashRecordsFile } from "../shared/schemas";
import { BashRuntime } from "./src/bash_runtime";

type Verdict = "ok" | "fail" | "fail-always";

type Held = { when: (file: BashRecordsFile) => boolean; hit: (file: BashRecordsFile) => void; verdict: Promise<Verdict> };

// Records-file renames are held at the durable boundary, in FIFO order, by predicate on the
// payload about to be published: until a held rename runs, the file keeps its previous content.
// Every attempted payload is logged.
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
          const verdict = await held.verdict;
          gate.failAlways = verdict === "fail-always";
          if (verdict !== "ok") throw eio();
        }
      }
      return fs.rename(from, to);
    },
  };
});

const holdsTerminal = (file: BashRecordsFile): boolean => Object.values(file.records).some((record) => record.status !== "running");

const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
const outcomes = ["ok", "fail", "fail-always"] as const;

async function until(predicate: () => boolean | Promise<boolean>): Promise<void> {
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) { if (await predicate()) return; await pause(5); }
  throw new Error("condition not reached");
}

/** True when the promise is still unresolved after a grace period. */
async function stillPending(promise: Promise<unknown>): Promise<boolean> {
  const marker = Symbol("pending");
  return (await Promise.race([promise.then(() => "settled", () => "settled"), pause(40).then(() => marker)])) === marker;
}

type Hold = { hit: Promise<BashRecordsFile>; settle: (verdict: Verdict) => void };
type Harness = ReturnType<typeof harness>;

function harness(root: string, runtime: BashRuntime) {
  const recordsPath = path.join(root, "pi-bash", "p-publication", "bash-records.json");
  const exitFlag = path.join(root, "exit");
  // The task exits only when the test lets it, after the gate is armed.
  const command = `until [ -e '${exitFlag}' ]; do sleep 0.01; done; printf done`;
  let latest: Hold = { hit: Promise.resolve({} as BashRecordsFile), settle: () => {} };
  return {
    runtime, command,
    /** Hold the next records write matching `when` (default: one publishing a terminal record). */
    arm(when: Held["when"] = holdsTerminal): Hold {
      const hold = {} as Hold;
      hold.hit = new Promise<BashRecordsFile>((hit) => {
        const verdict = new Promise<Verdict>((resolve) => { hold.settle = resolve; });
        gate.holds.push({ when, hit, verdict });
      });
      outstanding.push(hold);
      return latest = hold;
    },
    exit: (): Promise<void> => writeFile(exitFlag, ""),
    async exitAndHold(): Promise<void> { await this.exit(); await latest.hit; },
    /** Resolves once the task's process group is gone (its terminal write is then about to queue). */
    async gone(pid: number): Promise<void> {
      await until(() => { try { process.kill(-pid, 0); return false; } catch { return true; } });
      await pause(50);
    },
    settle: (verdict: Verdict): void => latest.settle(verdict),
    async pending(): Promise<string> {
      this.arm();
      const started = await runtime.execute({ command, background: true }, undefined) as { bash_id: string };
      await this.exitAndHold();
      return started.bash_id;
    },
    row: (id: string) => runtime.list(true).find((row) => row.bash_id === id)!,
    disk: async (id: string): Promise<BashRecord & { runtime_error?: string }> =>
      (JSON.parse(await readFile(recordsPath, "utf8")) as BashRecordsFile).records[id]!,
  };
}

const outstanding: Hold[] = [];

async function withRuntime(body: (h: Harness) => Promise<void>): Promise<void> {
  const root = await mkdtemp(path.join(tmpdir(), "pi-publication-"));
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", "p-publication");
  const runtime = new BashRuntime();
  try { await body(harness(root, runtime)); }
  finally {
    gate.holds.length = 0; gate.failAlways = false; gate.log.length = 0;
    for (const hold of outstanding.splice(0)) hold.settle("ok");
    await runtime.shutdown().catch(() => undefined);
    await rm(root, { recursive: true, force: true });
  }
}

afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); });

describe("managed Bash terminal publication", () => {
  it.each(["ok", "fail"] as const)("output and an interrupted wait return without consuming an unpublished result (%s)", async (verdict) => {
    await withRuntime(async (h) => {
      const id = await h.pending();
      expect(h.row(id)).toMatchObject({ status: "running", ended_at_ms: null });
      expect((await h.disk(id)).status).toBe("running");

      expect(await h.runtime.manage({ action: "output", bash_id: id })).toMatchObject({ output: "done" });
      const abort = new AbortController();
      const waiting = h.runtime.manage({ action: "wait", bash_id: id }, abort.signal);
      await pause(20);
      abort.abort();
      expect(await waiting).toMatchObject({ status: "running", message: expect.stringContaining("interrupted") });

      h.settle(verdict);
      // Failure evidence reaches memory before its repair write; wait for what the file says.
      await until(async () => (await h.disk(id)).status !== "running");
      const record = await h.disk(id);
      // The completion notice stays owed in both outcomes.
      expect(record.notification_consumed_at_ms).toBeUndefined();
      if (verdict === "ok") expect(record).toMatchObject({ status: "exited", exit_code: 0 });
      else expect(record).toMatchObject({ status: "killed", execution_error: expect.stringContaining("EIO") });
    });
  });

  it.each(outcomes)("overlapping waits and list follow the publication outcome (%s)", async (verdict) => {
    await withRuntime(async (h) => {
      const id = await h.pending();
      const waits = [h.runtime.manage({ action: "wait", bash_id: id }), h.runtime.manage({ action: "wait", bash_id: id })];
      const settled = waits.map((wait) => stillPending(wait));
      expect(await Promise.all(settled)).toEqual([true, true]);
      expect(h.row(id)).toMatchObject({ status: "running", exit_code: null });

      h.settle(verdict);
      if (verdict === "ok") {
        for (const result of await Promise.all(waits)) expect(result).toMatchObject({ status: "exited", exit_code: 0, output: "done" });
        expect(await h.disk(id)).toMatchObject({ status: "exited", notification_consumed_at_ms: expect.any(Number) });
        return;
      }
      for (const wait of waits) await expect(wait).rejects.toThrow("EIO");
      expect(h.row(id)).toMatchObject({ status: "killed", execution_error: expect.stringContaining("EIO") });
      // A persistent fault publishes nothing: the durable row is still the running one.
      const record = await h.disk(id);
      expect(record.notification_consumed_at_ms).toBeUndefined();
      expect(record.status).toBe(verdict === "fail" ? "killed" : "running");
    });
  });

  it.each(["ok", "fail"] as const)("kill reports the published outcome, not the exit (%s)", async (verdict) => {
    await withRuntime(async (h) => {
      const id = await h.pending();
      const groupSignals = vi.spyOn(process, "kill");
      const kill = h.runtime.manage({ action: "kill", bash_id: id });
      expect(await stillPending(kill)).toBe(true);
      h.settle(verdict);
      // The group was observed gone before publication; it is never signalled again (its id may be reused).
      const afterwards = async (): Promise<void> => expect(groupSignals.mock.calls.filter(([pid]) => pid < 0)).toEqual([]);
      if (verdict === "fail") { await expect(kill).rejects.toThrow("EIO"); return await afterwards(); }
      // The task exited on its own before the signal, so nothing was killed.
      expect(await kill).toMatchObject({ killed: false, message: `${id} is already exited` });
      expect(h.row(id).status).toBe("exited");
      await afterwards();
    });
  });

  it.each([
    ["execute", (h: Harness) => h.runtime.execute({ command: h.command }, undefined), { stdout: "done", exit_code: 0 }, { error: expect.stringContaining("EIO") }],
    ["executeUserBash", (h: Harness) => h.runtime.executeUserBash(h.command, process.cwd()), { exitCode: 0 }, { exitCode: -1 }],
  ] as const)("foreground %s returns only after its terminal write (%s)", async (_name, run, success, failure) => {
    for (const verdict of ["ok", "fail"] as const) {
      await withRuntime(async (h) => {
        h.arm();
        const result = run(h);
        await until(() => h.runtime.list(true).length === 1);
        await h.exitAndHold();
        expect(await stillPending(result)).toBe(true);
        h.settle(verdict);
        expect(await result).toMatchObject(verdict === "ok" ? success : failure);
      });
    }
  });

  it.each([
    ["execute", (h: Harness, signal: AbortSignal) => h.runtime.execute({ command: "sleep 30" }, signal), { stderr: "[command aborted]", exit_code: -1 }],
    ["executeUserBash", (h: Harness, signal: AbortSignal) => h.runtime.executeUserBash("sleep 30", process.cwd(), { signal }), { exitCode: -1 }],
  ] as const)("foreground %s abort returns after the physical stop while its terminal write is held", async (_name, run, aborted) => {
    await withRuntime(async (h) => {
      const hold = h.arm();
      const abort = new AbortController();
      const result = run(h, abort.signal);
      await until(() => h.runtime.list(true).length === 1);
      const { bash_id: id, pid } = h.row(h.runtime.list(true)[0]!.bash_id);
      abort.abort();
      expect(await result).toMatchObject(aborted);
      await hold.hit;
      // Stopped, but its outcome is not durable: still unpublished, not claimed, not consumed.
      expect(h.row(id)).toMatchObject({ status: "running", ended_at_ms: null });
      expect(() => process.kill(-pid!, 0)).toThrow();
      expect((await h.disk(id)).status).toBe("running");
      hold.settle("ok");
      await until(() => h.row(id).status !== "running");
      const record = await h.disk(id);
      expect(record.status).toBe("killed");
      expect(record.notification_consumed_at_ms).toBeUndefined();
    });
  });

  it("does not consume a result whose record fails in a lifecycle write queued ahead of the consumer", async () => {
    await withRuntime(async (h) => {
      const terminal = h.arm();
      const foreground = h.runtime.execute({ command: h.command }, undefined);
      await until(() => h.runtime.list(true).length === 1);
      const id = h.runtime.list(true)[0]!.bash_id;
      await h.exitAndHold();
      // Backgrounding publishes is_background behind the held terminal write.
      const backgrounding = h.runtime.backgroundForeground();
      const lifecycle = h.arm();
      terminal.settle("ok");
      await lifecycle.hit;
      // The terminal result is published; a waiter checks the record and queues its consumption
      // behind the held backgrounding write, which then fails and fails the record.
      const waiting = h.runtime.manage({ action: "wait", bash_id: id });
      await pause(60);
      lifecycle.settle("fail");
      await expect(backgrounding).rejects.toThrow("EIO");
      await waiting;
      await foreground;
      await until(async () => (await h.disk(id)).execution_error != null);
      // The error notice is still owed: a failed record is never marked consumed.
      expect((await h.disk(id)).notification_consumed_at_ms).toBeUndefined();
    });
  });

  it("a failed lifecycle write never leaves its change in a snapshot published by another record's slot", async () => {
    await withRuntime(async (h) => {
      const start = async (command: string) => (await h.runtime.execute({ command, background: true }, undefined) as { bash_id: string }).bash_id;
      const a = await start("sleep 30");
      const b = await start(h.command);
      // W0 (a creation write) is blocked while B exits and queues its terminal write W1, then A is detached (W2).
      const w0 = h.arm(() => true);
      const third = h.runtime.startDetachedUserBash("sleep 30", process.cwd());
      await w0.hit;
      const w1 = h.arm(holdsTerminal);
      const w2 = h.arm((file) => file.records[a]?.is_tracked === false);
      await h.exit();
      await h.gone(h.row(b).pid!);
      const detaching = h.runtime.manage({ action: "detach", bash_id: a });
      w0.settle("ok");

      // B's slot publishes B's exit and nothing speculative about A.
      const published = await w1.hit;
      expect(published.records[b]).toMatchObject({ status: "exited" });
      expect(published.records[a]!.is_tracked).toBe(true);
      w1.settle("ok");
      await w2.hit;
      w2.settle("fail");
      await expect(detaching).rejects.toThrow("EIO");
      expect(h.row(a).is_tracked).toBe(true);
      expect((await h.disk(a)).is_tracked).toBe(true);
      expect(await h.disk(b)).toMatchObject({ status: "exited" });
      await third; // its own backgrounding write queued behind the others
    });
  });

  it("a failure that claims the record before its terminal slot stages prevents a clean terminal snapshot", async () => {
    await withRuntime(async (h) => {
      const a = (await h.runtime.execute({ command: "sleep 30", background: true }, undefined) as { bash_id: string }).bash_id;
      // Public path: the background publication of a task that is exiting fails, ahead of its terminal write.
      const backgrounding = h.arm((file) => Object.values(file.records).some((r) => r.command === h.command && r.is_background));
      const started = h.runtime.execute({ command: h.command, background: true }, undefined).then(() => "started", (error: Error) => error.message);
      await backgrounding.hit;
      const b = h.runtime.list(true).find((row) => row.command === h.command)!;
      const between = h.arm((file) => file.records[a]?.is_tracked === false);
      const detaching = h.runtime.manage({ action: "detach", bash_id: a });
      await h.exit();
      await h.gone(b.pid!);

      backgrounding.settle("fail");
      await until(() => h.row(b.bash_id).execution_error != null);
      between.settle("ok");
      expect(await started).toContain("EIO");
      await detaching;
      await until(async () => (await h.disk(b.bash_id)).status === "killed");
      // The exit was never staged: no attempted snapshot published it, and the error stays owed.
      expect(gate.log.filter((file) => file.records[b.bash_id]?.status === "exited")).toEqual([]);
      expect(await h.disk(b.bash_id)).toMatchObject({ status: "killed", execution_error: expect.stringContaining("EIO") });
      expect((await h.disk(b.bash_id)).notification_consumed_at_ms).toBeUndefined();
    });
  });

  it("a failed ping claim claims its record before the terminal slot queued behind it stages", async () => {
    await withRuntime(async (h) => {
      vi.stubEnv("_MERIDIAN_PI_TASK_PING_INTERVAL_MS", "20");
      new BashRuntime({ onBackgroundPing: () => {} }); // the owner is shared: this only binds the ping hook
      const claim = h.arm((file) => Object.values(file.records).some((r) => r.command === h.command && r.ping_sent_at_ms != null));
      const started = await h.runtime.execute({ command: h.command, background: true }, undefined) as { bash_id: string };
      const id = started.bash_id;
      await claim.hit; // the claim write is held while the task still runs
      await h.exit();
      await h.gone(h.row(id).pid!); // the exit is observed and its terminal write queued behind the claim

      // The first non-running payload attempted from here is either the terminal success or the failure repair.
      const next = h.arm(holdsTerminal);
      claim.settle("fail");
      const attempted = await next.hit;
      expect(attempted.records[id]).toMatchObject({ status: "killed", execution_error: expect.stringContaining("EIO") });
      next.settle("fail"); // and the repair write fails too: the file must not keep a clean success
      await until(() => h.row(id).status === "killed");
      expect(gate.log.filter((file) => file.records[id]?.status === "exited")).toEqual([]);
      expect((await h.disk(id)).status).toBe("running");
    });
  });

  it("a failed clear leaves the finished rows in memory and on disk", async () => {
    await withRuntime(async (h) => {
      await h.runtime.execute({ command: "printf x" }, undefined);
      const id = h.runtime.list(true)[0]!.bash_id;
      const clearing = h.runtime.clearFinished();
      const hold = h.arm((file) => Object.keys(file.records).length === 0);
      await hold.hit;
      hold.settle("fail");
      await expect(clearing).rejects.toThrow("EIO");
      expect(h.runtime.list(true)).toHaveLength(1);
      expect(await h.disk(id)).toMatchObject({ status: "exited" });
    });
  });

  it("recovers a terminal write that never landed as lost ownership, not as an exit", async () => {
    await withRuntime(async (h) => {
      const id = await h.pending();
      const waiting = h.runtime.manage({ action: "wait", bash_id: id });
      h.settle("fail-always");
      await expect(waiting).rejects.toThrow("EIO");
      gate.failAlways = false;

      const owners = (globalThis as Record<symbol, Map<string, BashRuntime>>)[Symbol.for("meridian.pi.managed-bash.owners.v2")]!;
      owners.clear();
      const restarted = new BashRuntime();
      expect(await restarted.manage({ action: "list", include_completed: true })).toMatchObject({
        rows: [expect.objectContaining({ bash_id: id, status: "running", execution_error: expect.stringContaining("ownership_lost") })],
      });
      await expect(restarted.manage({ action: "wait", bash_id: id })).rejects.toThrow("ownership_lost");
      expect((await h.disk(id)).notification_consumed_at_ms).toBeUndefined();
    });
  });
});
