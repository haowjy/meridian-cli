import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BashRecordsFile } from "../shared/schemas";
import { BashLogStore } from "./src/bash_log_store";
import type { Publication } from "./src/bash_publication";
import { BashRuntime, type BashExecuteResult } from "./src/bash_runtime";
import { until } from "../shared/test_wait";

type Verdict = "ok" | "fail";
type Held = {
  when: (file: BashRecordsFile) => boolean;
  hit: (file: BashRecordsFile) => void;
  verdict: Promise<Verdict>;
};

const gate = vi.hoisted(() => ({ holds: [] as Held[], writes: [] as BashRecordsFile[] }));

vi.mock("node:fs/promises", async (importOriginal) => {
  const fs = await importOriginal<typeof import("node:fs/promises")>();
  return {
    ...fs,
    rename: async (from: string, to: string) => {
      if (String(to).endsWith("bash-records.json")) {
        const file = JSON.parse(await fs.readFile(from, "utf8")) as BashRecordsFile;
        gate.writes.push(file);
        if (gate.holds[0]?.when(file)) {
          const held = gate.holds.shift()!;
          held.hit(file);
          if (await held.verdict === "fail") {
            throw Object.assign(new Error("EIO: injected creation write failure"), { code: "EIO" });
          }
        }
      }
      return fs.rename(from, to);
    },
  };
});

const SPAWN_ID = "p-creation-publication";
const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

function deferred(): { promise: Promise<void>; resolve(): void } {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

type Hold = { hit: Promise<BashRecordsFile>; settle(verdict: Verdict): void };
type Publisher = { publish(publication: Publication): Promise<boolean> };
type Harness = Awaited<ReturnType<typeof openHarness>>;

async function openHarness(root: string) {
  const exitFlag = path.join(root, "cooperative-exit");
  const ready = path.join(root, "ready");
  const recordsPath = path.join(root, "pi-bash", SPAWN_ID, "bash-records.json");
  const requests: Array<Promise<unknown>> = [];
  const holds: Hold[] = [];
  const runtime = new BashRuntime();
  const h = {
    root,
    runtime,
    exitFlag,
    ready,
    longCommand: `until [ -e '${exitFlag}' ]; do sleep 0.01; done; printf done`,
    abortCommand: `trap 'printf stopped; exit 0' TERM; printf started; touch '${ready}'; while [ ! -e '${exitFlag}' ]; do printf x; sleep 0.01; done; printf done`,
    request<T>(request: Promise<T>): Promise<T> {
      requests.push(request.catch(() => undefined));
      return request;
    },
    armCreation(): Hold {
      const hold = {} as Hold;
      hold.hit = new Promise<BashRecordsFile>((hit) => {
        const verdict = new Promise<Verdict>((resolve) => { hold.settle = resolve; });
        gate.holds.push({
          when: (file) => Object.keys(file.records).length > 0,
          hit,
          verdict,
        });
      });
      holds.push(hold);
      return hold;
    },
    publisher(): Publisher {
      return (runtime as unknown as { publication: Publisher }).publication;
    },
    async disk() {
      return JSON.parse(await readFile(recordsPath, "utf8")) as BashRecordsFile;
    },
    async teardown(): Promise<void> {
      gate.holds.length = 0;
      for (const hold of holds) hold.settle("ok");
      await writeFile(exitFlag, "").catch(() => undefined);
      await Promise.allSettled(requests);
      await runtime.shutdown().catch(() => undefined);
    },
  };
  return h;
}

async function withRuntime(body: (h: Harness) => Promise<void>): Promise<void> {
  const root = await mkdtemp(path.join(tmpdir(), "pi-creation-publication-"));
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", SPAWN_ID);
  gate.holds.length = 0;
  gate.writes.length = 0;
  const h = await openHarness(root);
  try {
    await body(h);
  } finally {
    await h.teardown();
    gate.writes.length = 0;
    await rm(root, { recursive: true, force: true });
  }
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("foreground ownership before creation publication", () => {
  it("model abort stops, drains, and returns before a held creation lands", async () => {
    await withRuntime(async (h) => {
      const creation = h.armCreation();
      const abort = new AbortController();
      const request = h.request(h.runtime.execute({ command: h.abortCommand }, abort.signal));
      const proposed = await creation.hit;
      const row = Object.values(proposed.records)[0]!;
      await until(() => readFile(h.ready).then(() => true, () => false));

      abort.abort();
      const result = await request;
      expect(result).toMatchObject({
        kind: "foreground", status: "aborted", exit_code: -1,
        stdout: { kind: "available", text: expect.stringMatching(/started.*stopped$/s), truncated: false },
        stderr: { kind: "available", text: expect.any(String), truncated: false },
      });
      expect((result as Extract<BashExecuteResult, { kind: "foreground" }>).stdout).toEqual({
        kind: "available", text: await readFile(row.stdout_log_path, "utf8"), truncated: false,
      });

      creation.settle("ok");
      await until(async () => {
        try { return Object.values((await h.disk()).records)[0]?.status === "killed"; }
        catch { return false; }
      });
    });
  });

  it("human abort stops, drains, and returns before a held creation later fails", async () => {
    await withRuntime(async (h) => {
      const unhandled: unknown[] = [];
      const listener = (error: unknown): void => { unhandled.push(error); };
      process.on("unhandledRejection", listener);
      const creation = h.armCreation();
      const abort = new AbortController();
      const output: Buffer[] = [];
      try {
        const request = h.request(h.runtime.executeUserBash(h.abortCommand, h.root, {
          signal: abort.signal,
          onData: (chunk) => output.push(Buffer.from(chunk)),
        }));
        const proposed = await creation.hit;
        const row = Object.values(proposed.records)[0]!;
        await until(() => readFile(h.ready).then(() => true, () => false));

        abort.abort();
        expect(await request).toEqual({ exitCode: -1 });
        expect(Buffer.concat(output).toString("utf8")).toMatch(/^started/);
        expect(await readFile(row.stdout_log_path, "utf8")).toMatch(/started.*stopped$/s);

        creation.settle("fail");
        await until(async () => {
          try { return Object.values((await h.disk()).records)[0]?.execution_error?.includes("EIO") === true; }
          catch { return false; }
        });
        expect(Object.values((await h.disk()).records)[0]).toMatchObject({ status: "killed" });
        expect(unhandled).toEqual([]);
      } finally {
        process.removeListener("unhandledRejection", listener);
      }
    });
  });
});

describe("pre-launch cancellation", () => {
  it.each(["model", "human"] as const)("does not launch %s Bash after cancellation during preparation", async (kind) => {
    await withRuntime(async (h) => {
      const marker = path.join(h.root, `UNMISTAKABLE-${kind}-PROCESS-SIDE-EFFECT`);
      const entered = deferred();
      const release = deferred();
      const store = (h.runtime as unknown as { logStore: BashLogStore }).logStore;
      const create = store.create.bind(store);
      vi.spyOn(store, "create").mockImplementationOnce(async (bashId) => {
        entered.resolve();
        await release.promise;
        return create(bashId);
      });
      const abort = new AbortController();
      const command = `printf launched > '${marker}'`;
      const request = kind === "model"
        ? h.request(h.runtime.execute({ command, background: true }, abort.signal))
        : h.request(h.runtime.executeUserBash(command, h.root, { signal: abort.signal }));

      await entered.promise;
      abort.abort();
      release.resolve();
      if (kind === "model") {
        expect(await request).toMatchObject({ kind: "foreground", status: "aborted", exit_code: -1 });
      } else {
        expect(await request).toEqual({ exitCode: -1 });
      }
      await expect(readFile(marker)).rejects.toMatchObject({ code: "ENOENT" });
      expect(h.runtime.list(true)).toEqual([]);
    });
  });
});

describe("positive publication authority", () => {
  it.each([
    ["short", "printf short", "short"],
    ["long", null, "done"],
  ] as const)("normal %s foreground waits through terminal publication", async (_name, literal, output) => {
    await withRuntime(async (h) => {
      const command = literal ?? h.longCommand;
      const request = h.request(h.runtime.execute({ command }, undefined));
      if (literal === null) {
        await until(async () => {
          try { return Object.values((await h.disk()).records)[0]?.status === "running"; }
          catch { return false; }
        });
        await writeFile(h.exitFlag, "");
      }
      expect(await request).toMatchObject({
        kind: "foreground", status: "exited", exit_code: 0,
        stdout: { kind: "available", text: output, truncated: false },
      });
      expect(Object.values((await h.disk()).records)[0]).toMatchObject({ status: "exited", exit_code: 0 });
    });
  });

  it.each(["ok", "fail"] as const)("background start reports only a %s creation publication", async (verdict) => {
    await withRuntime(async (h) => {
      const creation = h.armCreation();
      const request = h.request(h.runtime.execute({ command: h.longCommand, background: true }, undefined));
      const proposed = await creation.hit;
      expect(Object.values(proposed.records)[0]).toMatchObject({ status: "running", is_background: false });
      creation.settle(verdict);
      if (verdict === "ok") {
        expect(await request).toMatchObject({ kind: "start", status: "started" });
        expect(Object.values((await h.disk()).records)[0]).toMatchObject({ status: "running", is_background: true });
      } else {
        await expect(request).rejects.toThrow("EIO");
        await until(async () => Object.values((await h.disk()).records)[0]?.execution_error?.includes("EIO") === true);
        expect(Object.values((await h.disk()).records)[0]).toMatchObject({ status: "killed", is_background: false });
      }
    });
  });

  it.each(["ok", "fail"] as const)("early foreground handoff follows a %s creation publication", async (verdict) => {
    await withRuntime(async (h) => {
      const creation = h.armCreation();
      const foreground = h.request(h.runtime.execute({ command: h.longCommand }, undefined));
      await creation.hit;
      const handoff = h.request(h.runtime.backgroundForeground());
      creation.settle(verdict);

      if (verdict === "ok") {
        expect(await handoff).toMatchObject({ ok: true, bash_id: expect.any(String) });
        expect(await foreground).toMatchObject({ kind: "start", status: "backgrounded" });
        expect(Object.values((await h.disk()).records)[0]).toMatchObject({ status: "running", is_background: true });
      } else {
        expect(await handoff).toMatchObject({ ok: false });
        await expect(foreground).rejects.toThrow("EIO");
        await until(async () => Object.values((await h.disk()).records)[0]?.execution_error?.includes("EIO") === true);
        expect(gate.writes.some((file) => Object.values(file.records)[0]?.is_background === true)).toBe(false);
      }
    });
  });

  it("declines an early handoff queued after terminal observation", async () => {
    await withRuntime(async (h) => {
      const creation = h.armCreation();
      const publisher = h.publisher();
      const originalPublish = publisher.publish.bind(publisher);
      const terminalQueued = deferred();
      let calls = 0;
      publisher.publish = (publication) => {
        const result = originalPublish(publication);
        calls += 1;
        if (calls === 2) terminalQueued.resolve();
        return result;
      };
      const foreground = h.request(h.runtime.execute({ command: "printf done" }, undefined));
      await creation.hit;
      await terminalQueued.promise;
      const handoff = h.request(h.runtime.backgroundForeground());
      creation.settle("ok");

      expect(await handoff).toMatchObject({ ok: false });
      expect(await foreground).toMatchObject({ kind: "foreground", status: "exited", exit_code: 0 });
      expect(Object.values((await h.disk()).records)[0]).toMatchObject({ status: "exited", is_background: false });
      expect(gate.writes.some((file) => Object.values(file.records)[0]?.is_background === true)).toBe(false);
    });
  });

  it("timeout handoff can queue behind creation and reports only its adopted transition", async () => {
    await withRuntime(async (h) => {
      const realSetTimeout = globalThis.setTimeout;
      let fireTimeout: (() => void) | undefined;
      vi.spyOn(globalThis, "setTimeout").mockImplementation(((callback: (...args: unknown[]) => void, ms?: number, ...args: unknown[]) => {
        if (ms === 60_000) {
          fireTimeout = () => callback(...args);
          return realSetTimeout(() => undefined, ms);
        }
        return realSetTimeout(callback, ms, ...args);
      }) as typeof setTimeout);
      const creation = h.armCreation();
      const foreground = h.request(h.runtime.execute({ command: h.longCommand, timeout_min: 1 }, undefined));
      await creation.hit;
      expect(fireTimeout).toBeDefined();
      fireTimeout!();
      creation.settle("ok");

      expect(await foreground).toMatchObject({ kind: "start", status: "backgrounded" });
      expect(Object.values((await h.disk()).records)[0]).toMatchObject({ status: "running", is_background: true });
    });
  });
});
