import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BashRecord, BashRecordsFile } from "../shared/schemas";
import { BashRuntime } from "./src/bash_runtime";

type Verdict = "ok" | "fail" | "fail-always";

// The first records-file rename that would publish a terminal record is held at the
// durable boundary: until it runs, the file still says "running".
const gate = vi.hoisted(() => ({
  armed: false,
  failAlways: false,
  hit: () => {},
  verdict: Promise.resolve("ok") as Promise<Verdict>,
}));

vi.mock("node:fs/promises", async (importOriginal) => {
  const fs = await importOriginal<typeof import("node:fs/promises")>();
  const eio = (): Error => Object.assign(new Error("EIO: injected records write failure"), { code: "EIO" });
  return {
    ...fs,
    rename: async (from: string, to: string) => {
      if (String(to).endsWith("bash-records.json")) {
        if (gate.failAlways) throw eio();
        if (gate.armed) {
          const file = JSON.parse(await fs.readFile(from, "utf8")) as BashRecordsFile;
          if (Object.values(file.records).some((record) => record.status !== "running")) {
            gate.armed = false;
            gate.hit();
            const verdict = await gate.verdict;
            gate.failAlways = verdict === "fail-always";
            if (verdict !== "ok") throw eio();
          }
        }
      }
      return fs.rename(from, to);
    },
  };
});

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

type Harness = ReturnType<typeof harness>;

function harness(root: string, runtime: BashRuntime) {
  const recordsPath = path.join(root, "pi-bash", "p-publication", "bash-records.json");
  const exitFlag = path.join(root, "exit");
  // The task exits only when the test lets it, after the gate is armed.
  const command = `until [ -e '${exitFlag}' ]; do sleep 0.01; done; printf done`;
  let release: (verdict: Verdict) => void = () => {};
  let held = Promise.resolve();
  return {
    runtime, command,
    /** Hold the next terminal publication; resolves once the task's exit is held at the write. */
    arm(): void {
      gate.armed = true;
      gate.verdict = new Promise<Verdict>((resolve) => { release = resolve; });
      held = new Promise<void>((resolve) => { gate.hit = resolve; });
    },
    async exitAndHold(): Promise<void> { await writeFile(exitFlag, ""); await held; },
    settle: (verdict: Verdict): void => release(verdict),
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

async function withRuntime(body: (h: Harness) => Promise<void>): Promise<void> {
  const root = await mkdtemp(path.join(tmpdir(), "pi-publication-"));
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", "p-publication");
  const runtime = new BashRuntime();
  try { await body(harness(root, runtime)); }
  finally {
    gate.armed = false; gate.failAlways = false; gate.hit(); gate.hit = () => {};
    await runtime.shutdown().catch(() => undefined);
    await rm(root, { recursive: true, force: true });
  }
}

afterEach(() => vi.unstubAllEnvs());

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
      await until(() => h.row(id).status !== "running");
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
      const kill = h.runtime.manage({ action: "kill", bash_id: id });
      expect(await stillPending(kill)).toBe(true);
      h.settle(verdict);
      if (verdict === "fail") return await expect(kill).rejects.toThrow("EIO");
      // The task exited on its own before the signal, so nothing was killed.
      expect(await kill).toMatchObject({ killed: false, message: `${id} is already exited` });
      expect(h.row(id).status).toBe("exited");
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
