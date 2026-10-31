import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BashRecord, BashRecordsFile } from "../shared/schemas";
import { BashRuntime, type BashListRow } from "./src/bash_runtime";
import { until } from "../shared/test_wait";

type Verdict = "ok" | "fail" | "fail-always";

type Held = { when: (file: BashRecordsFile) => boolean; hit: (file: BashRecordsFile) => void; verdict: Promise<Verdict> };

// Records-file renames are held at the durable boundary, in FIFO order, by predicate on the
// payload about to be published: until a held rename runs, the file keeps its previous content.
// Every attempted payload is logged.
const gate = vi.hoisted(() => ({ holds: [] as Held[], failAlways: false, failAppendPath: null as string | null, log: [] as BashRecordsFile[] }));

vi.mock("node:fs/promises", async (importOriginal) => {
  const fs = await importOriginal<typeof import("node:fs/promises")>();
  const eio = (): Error => Object.assign(new Error("EIO: injected records write failure"), { code: "EIO" });
  return {
    ...fs,
    writeFile: async (...args: Parameters<typeof fs.writeFile>) => {
      if (String(args[0]) === gate.failAppendPath) throw new Error("EIO: injected log append failure");
      return fs.writeFile(...args);
    },
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
    runtime, command, root,
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
    /** A pipe failure on a live task: the very listener a real stdout error reaches, outside any publication slot. */
    fault(id: string, message: string): void {
      const live = (runtime as unknown as { executions: Map<string, { task: { child: { stdout: NodeJS.EventEmitter } } | null }> }).executions.get(id);
      live!.task!.child.stdout.emit("error", new Error(message));
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
  gate.log.length = 0;
  try { await body(harness(root, runtime)); }
  finally {
    gate.holds.length = 0; gate.failAlways = false; gate.failAppendPath = null;
    for (const hold of outstanding.splice(0)) hold.settle("ok");
    await runtime.shutdown().catch(() => undefined);
    gate.log.length = 0; // shutdown publishes too
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
      for (const wait of waits) expect(await wait).toMatchObject({ kind: "wait", error: expect.stringContaining("EIO") });
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
    ["execute", (h: Harness) => h.runtime.execute({ command: h.command }, undefined), { stdout: { kind: "available", text: "done" }, exit_code: 0 }, { error: expect.stringContaining("EIO") }],
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
    ["execute", (h: Harness, signal: AbortSignal) => h.runtime.execute({ command: "sleep 30" }, signal), {
      kind: "foreground", status: "aborted", exit_code: -1,
      stdout: { kind: "available", text: "", truncated: false },
      stderr: { kind: "available", text: "", truncated: false },
    }],
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

  it("declines a background handoff queued behind terminal publication", async () => {
    await withRuntime(async (h) => {
      const terminal = h.arm();
      const foreground = h.runtime.execute({ command: h.command }, undefined);
      await until(() => h.runtime.list(true).length === 1);
      const id = h.runtime.list(true)[0]!.bash_id;
      await h.exitAndHold();
      // Backgrounding publishes is_background behind the held terminal write.
      const backgrounding = h.runtime.backgroundForeground();
      terminal.settle("ok");
      expect(await backgrounding).toMatchObject({ ok: false, reason: "foreground_not_published" });
      expect(await foreground).toMatchObject({ kind: "foreground", status: "exited", exit_code: 0 });
      expect(await h.disk(id)).toMatchObject({ status: "exited", is_background: false });
      expect(gate.log.filter((file) => file.records[id]?.is_background === true)).toEqual([]);
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

  it("a fault and a creation arriving during another record's write survive its adoption", async () => {
    await withRuntime(async (h) => {
      const start = async (command: string) => (await h.runtime.execute({ command, background: true }, undefined) as { bash_id: string }).bash_id;
      const a = await start("sleep 30");
      const b = await start("sleep 30");
      const write = h.arm((file) => file.records[a]?.is_tracked === false);
      const detaching = h.runtime.manage({ action: "detach", bash_id: a });
      const staged = await write.hit;

      // While A's snapshot is in flight: a new task is created and B faults outside the queue.
      const creating = h.runtime.startDetachedUserBash("sleep 30", process.cwd());
      await until(() => h.runtime.list(true).length === 3);
      h.fault(b, "EIO: pipe broke");
      expect(h.row(b).execution_error).toContain("pipe broke");
      // Failure precedence is by staging time: the write in flight never saw the fault.
      expect(staged.records[b]!.execution_error).toBeUndefined();

      write.settle("ok");
      await detaching;
      const c = (await creating).bash_id;
      // Adopting A's snapshot lost neither the new row nor the live fault.
      expect(h.row(b).execution_error).toContain("pipe broke");
      expect(h.row(c)).toMatchObject({ status: "running", is_background: true });
      await until(() => h.row(b).status === "killed");
      await until(async () => (await h.disk(b)).status === "killed");
      expect(await h.disk(a)).toMatchObject({ is_tracked: false, status: "running" });
      expect(await h.disk(b)).toMatchObject({ status: "killed", execution_error: expect.stringContaining("pipe broke") });
      expect(await h.disk(c)).toMatchObject({ status: "running", is_background: true });
      // Only published metadata reaches the file: no live handle, progress or fault latch is serialized.
      const live = ["task", "outputQueue", "finished", "fault", "repair", "proposal", "logPaths", "logBytes", "pingTimer", "foregroundFinish", "terminationReason"];
      for (const file of gate.log) for (const record of Object.values(file.records)) expect(Object.keys(record).filter((key) => live.includes(key))).toEqual([]);
    });
  });

  it("a fault claimed while the staged success is being written still owns the outcome", async () => {
    await withRuntime(async (h) => {
      const id = await h.pending(); // the exit is observed and its terminal write is in flight
      const waiting = h.runtime.manage({ action: "wait", bash_id: id });
      expect(await stillPending(waiting)).toBe(true);
      h.fault(id, "EIO: late pipe error");
      await until(() => h.row(id).status === "killed");
      expect(h.row(id).execution_error).toContain("late pipe error");

      h.settle("ok");
      expect(await waiting).toMatchObject({ kind: "wait", error: expect.stringContaining("late pipe error") });
      await until(async () => (await h.disk(id)).status === "killed");
      expect(await h.disk(id)).toMatchObject({ status: "killed", execution_error: expect.stringContaining("late pipe error") });
      expect((await h.disk(id)).notification_consumed_at_ms).toBeUndefined();
      // The window is not closed by construction: the staged success did land first, then was repaired.
      const attempted = gate.log.filter((file) => file.records[id]?.status !== "running").map((file) => file.records[id]!.status);
      expect(attempted).toEqual(["exited", "killed"]);
    });
  });

  it("a failed detach or consume is the caller's error and never faults the task", async () => {
    await withRuntime(async (h) => {
      const started = async (command: string) => (await h.runtime.execute({ command, background: true }, undefined) as { bash_id: string }).bash_id;
      const a = await started("sleep 30");
      const detach = h.arm((file) => file.records[a]?.is_tracked === false);
      const detaching = h.runtime.manage({ action: "detach", bash_id: a });
      await detach.hit;
      detach.settle("fail");
      await expect(detaching).rejects.toThrow("EIO");
      expect(h.row(a)).toMatchObject({ status: "running", is_tracked: true });
      expect(h.row(a).execution_error).toBeUndefined();
      expect(() => process.kill(-h.row(a).pid!, 0)).not.toThrow();
      expect(await h.runtime.manage({ action: "detach", bash_id: a })).toMatchObject({ detached: true });
      expect(await h.disk(a)).toMatchObject({ is_tracked: false, status: "running" });

      const b = await started("printf x");
      await until(() => h.row(b).status === "exited");
      const consume = h.arm((file) => file.records[b]?.notification_consumed_at_ms != null);
      const reading = h.runtime.manage({ action: "output", bash_id: b });
      await consume.hit;
      consume.settle("fail");
      await expect(reading).rejects.toThrow("EIO");
      expect(h.row(b).status).toBe("exited");
      expect(h.row(b).execution_error).toBeUndefined();
      expect((await h.disk(b)).notification_consumed_at_ms).toBeUndefined();
      expect(await h.runtime.manage({ action: "output", bash_id: b })).toMatchObject({ output: "x" });
      expect((await h.disk(b)).notification_consumed_at_ms).toEqual(expect.any(Number));
      // Nothing was claimed: no failure notice was ever carried to the file.
      expect(gate.log.every((file) => file.runtime_error === undefined)).toBe(true);
    });
  });

  it("a creation write that fails claims the record in its slot and publishes the failure evidence", async () => {
    await withRuntime(async (h) => {
      const creation = h.arm((file) => Object.keys(file.records).length > 0);
      const starting = h.runtime.execute({ command: "sleep 30" }, undefined);
      await creation.hit;
      const { bash_id: id, pid } = h.runtime.list(true)[0]!;
      creation.settle("fail");
      await expect(starting).rejects.toThrow("EIO");
      // The unpublished proposal gets its repair row, and the task it started is gone.
      expect(await h.disk(id)).toMatchObject({ status: "killed", execution_error: expect.stringContaining("EIO") });
      expect(() => process.kill(-pid!, 0)).toThrow();
    });
  });

  it("foreground abort returns while an unrelated lifecycle write is blocked", async () => {
    await withRuntime(async (h) => {
      const a = (await h.runtime.execute({ command: "sleep 30", background: true }, undefined) as { bash_id: string }).bash_id;
      const abort = new AbortController();
      const foreground = h.runtime.execute({ command: "sleep 30" }, abort.signal);
      await until(() => h.runtime.list(true).length === 2);
      const blocked = h.arm((file) => file.records[a]?.is_tracked === false);
      const detaching = h.runtime.manage({ action: "detach", bash_id: a });
      await blocked.hit;
      abort.abort();
      expect(await foreground).toEqual({
        kind: "foreground", status: "aborted", exit_code: -1,
        stdout: { kind: "available", text: "", truncated: false },
        stderr: { kind: "available", text: "", truncated: false },
      });
      blocked.settle("ok");
      await detaching;
      const id = h.runtime.list(true).find((row) => row.bash_id !== a)!.bash_id;
      await until(async () => (await h.disk(id)).status === "killed");
    });
  });

  it("claims an unprintable callback failure instead of losing the fault during formatting", async () => {
    await withRuntime(async (h) => {
      const result = h.runtime.executeUserBash(h.command, process.cwd(), { onData: () => { throw Object.create(null); } });
      await until(() => h.runtime.list(true).length === 1);
      await h.exit();
      expect(await result).toEqual({ exitCode: -1 });
      const id = h.runtime.list(true)[0]!.bash_id;
      expect(await h.disk(id)).toMatchObject({ status: "killed", execution_error: "unprintable failure" });
    });
  });

  it("foreground abort drains failed output without waiting for its metadata repair", async () => {
    await withRuntime(async (h) => {
      const a = (await h.runtime.execute({ command: "sleep 30", background: true }, undefined) as { bash_id: string }).bash_id;
      const ready = path.join(h.root, "trap-ready");
      const abort = new AbortController();
      let returned = false;
      const foreground = h.runtime.execute({ command: `trap 'printf final-output; exit 0' TERM; touch '${ready}'; while :; do sleep 0.01; done` }, abort.signal)
        .then((result) => { returned = true; return result; });
      await until(async () => readFile(ready).then(() => true, () => false));
      const row = h.runtime.list(true).find((item) => item.bash_id !== a)!;
      const blocked = h.arm((file) => file.records[a]?.is_tracked === false);
      const detaching = h.runtime.manage({ action: "detach", bash_id: a });
      await blocked.hit;
      gate.failAppendPath = row.log_path;
      abort.abort();
      // The fault's physical cleanup completed, but its repair is behind the held detach.
      await until(() => h.row(row.bash_id).status === "killed");
      expect(h.row(row.bash_id).execution_error).toContain("log append failure");
      await until(() => returned);
      expect(await foreground).toMatchObject({ exit_code: -1 });
      expect((await h.disk(row.bash_id)).status).toBe("running");
      blocked.settle("ok");
      await detaching;
      await until(async () => (await h.disk(row.bash_id)).execution_error?.includes("log append failure") === true);
    });
  });

  it("recovery that cannot publish its diagnostic adopts nothing, then retries without signalling", async () => {
    await withRuntime(async (h) => {
      const bashId = "b-cafe0001";
      const dir = path.join(h.root, "pi-bash", "p-publication");
      await mkdir(dir, { recursive: true });
      const stored: BashRecord = {
        bash_id: bashId, command: "sleep 30", cwd: "/", pid: null, status: "running", is_background: true, is_tracked: true,
        exit_code: null, started_at_ms: 1, ended_at_ms: null, log_path: "l", stdout_log_path: "o", stderr_log_path: "e",
        log_bytes: 3, timeout_min: 1, execution_error: "earlier fault",
      };
      await writeFile(path.join(dir, "bash-records.json"), JSON.stringify({ v: 1, spawn_id: "p-publication", updated_at_ms: 1, runtime_error: "earlier runtime error", records: { [bashId]: stored } }));
      const signals = vi.spyOn(process, "kill");

      const owners = (globalThis as Record<symbol, Map<string, BashRuntime>>)[Symbol.for("meridian.pi.managed-bash.owners.v2")]!;
      owners.clear();
      gate.failAlways = true;
      const restarted = new BashRuntime();
      await expect(restarted.manage({ action: "list", include_completed: true })).rejects.toThrow("EIO");
      expect(restarted.list(true)).toEqual([]); // the failed publication adopted nothing
      expect(gate.log[0]).toMatchObject({ runtime_error: "earlier runtime error" });

      gate.failAlways = false;
      const listed = await restarted.manage({ action: "list", include_completed: true }) as { rows: BashListRow[] };
      expect(listed.rows).toHaveLength(1);
      expect(listed.rows[0]).toMatchObject({ bash_id: bashId, status: "running", log_bytes: 3 });
      expect(listed.rows[0]!.execution_error).toMatch(/^earlier fault; ownership_lost/);
      expect(await restarted.manage({ action: "kill", bash_id: bashId })).toMatchObject({ killed: false, message: expect.stringContaining("no PID was signalled") });
      expect(signals).not.toHaveBeenCalled();
      expect((await h.disk(bashId)).execution_error?.match(/ownership_lost/g)).toHaveLength(1);
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
      expect(await waiting).toMatchObject({ kind: "wait", error: expect.stringContaining("EIO") });
      gate.failAlways = false;

      const owners = (globalThis as Record<symbol, Map<string, BashRuntime>>)[Symbol.for("meridian.pi.managed-bash.owners.v2")]!;
      owners.clear();
      const restarted = new BashRuntime();
      expect(await restarted.manage({ action: "list", include_completed: true })).toMatchObject({
        rows: [expect.objectContaining({ bash_id: id, status: "running", execution_error: expect.stringContaining("ownership_lost") })],
      });
      expect(await restarted.manage({ action: "wait", bash_id: id })).toMatchObject({ kind: "wait", error: expect.stringContaining("ownership_lost") });
      expect((await h.disk(id)).notification_consumed_at_ms).toBeUndefined();
    });
  });
});
