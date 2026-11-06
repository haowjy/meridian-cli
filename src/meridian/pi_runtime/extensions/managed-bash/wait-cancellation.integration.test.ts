import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, expect, it, vi } from "vitest";

import type { BashRecord, BashRecordsFile } from "../shared/schemas";
import type { Published, Publication } from "./src/bash_publication";
import { BashLogStore } from "./src/bash_log_store";
import { BashRuntime } from "./src/bash_runtime";

type Publisher = {
  publish(publication: Publication): Promise<boolean>;
  write(next: Published): Promise<void>;
};

const roots: string[] = [];

async function terminalFixture(): Promise<{
  runtime: BashRuntime;
  recordsPath: string;
  bashId: string;
  publisher: Publisher;
}> {
  const root = await mkdtemp(path.join(tmpdir(), "bash-wait-cancel-"));
  roots.push(root);
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", `p-${path.basename(root)}`);
  const bashId = "b-00000001";
  const dir = path.join(root, "pi-bash", process.env.MERIDIAN_SPAWN_ID!);
  const log = path.join(root, "output.log");
  const record: BashRecord = {
    bash_id: bashId, command: "terminal fixture; not executed", cwd: root, pid: null,
    status: "exited", exit_code: 0, is_background: true, is_tracked: true,
    started_at_ms: 1, ended_at_ms: 2, log_path: log, stdout_log_path: log,
    stderr_log_path: log, log_bytes: 4, timeout_min: 55,
  };
  const recordsPath = path.join(dir, "bash-records.json");
  await mkdir(dir, { recursive: true });
  await writeFile(log, "done");
  await writeFile(recordsPath, JSON.stringify({
    v: 1, spawn_id: process.env.MERIDIAN_SPAWN_ID, updated_at_ms: 1, records: { [bashId]: record },
  }));
  const runtime = new BashRuntime();
  await runtime.rows();
  const publisher = (runtime as unknown as { publication: Publisher }).publication;
  return { runtime, recordsPath, bashId, publisher };
}

async function assertOwed(recordsPath: string, bashId: string): Promise<void> {
  const file = JSON.parse(await readFile(recordsPath, "utf8")) as BashRecordsFile;
  expect(file.records[bashId]!.notification_consumed_at_ms).toBeUndefined();
}

function deferred(): { promise: Promise<void>; resolve(): void } {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

afterEach(async () => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true });
});

it("leaves a terminal result owed when wait starts already aborted", async () => {
  const { runtime, recordsPath, bashId } = await terminalFixture();
  const abort = new AbortController();
  abort.abort();
  try {
    expect(await runtime.manage({ action: "wait", bash_id: bashId }, abort.signal)).toMatchObject({
      kind: "wait", delivery: "pending", status: "exited", exit_code: 0,
      message: expect.stringContaining("result remains owed"),
    });
    await assertOwed(recordsPath, bashId);
  } finally { await runtime.shutdown(); }
});

it("checks cancellation after a terminal log read and before acknowledgement", async () => {
  const { runtime, recordsPath, bashId } = await terminalFixture();
  const readStarted = deferred();
  const releaseRead = deferred();
  const original = BashLogStore.prototype.readResult;
  vi.spyOn(BashLogStore.prototype, "readResult").mockImplementationOnce(async function (
    this: BashLogStore,
    ...args: Parameters<BashLogStore["readResult"]>
  ) {
    readStarted.resolve();
    await releaseRead.promise;
    return original.apply(this, args);
  });
  const abort = new AbortController();
  try {
    const waiting = runtime.manage({ action: "wait", bash_id: bashId }, abort.signal);
    await readStarted.promise;
    abort.abort();
    releaseRead.resolve();
    expect(await waiting).toMatchObject({ delivery: "pending", status: "exited", exit_code: 0 });
    await assertOwed(recordsPath, bashId);
  } finally { releaseRead.resolve(); await runtime.shutdown(); }
});

it("checks cancellation when acknowledgement is queued behind an earlier publication", async () => {
  const { runtime, recordsPath, bashId, publisher } = await terminalFixture();
  const writeStarted = deferred();
  const releaseWrite = deferred();
  const acknowledgementQueued = deferred();
  const originalPublish = publisher.publish.bind(publisher);
  const originalWrite = publisher.write.bind(publisher);
  let hold = true;
  let observeAcknowledgement = false;
  publisher.publish = (publication) => {
    const queued = originalPublish(publication);
    if (observeAcknowledgement) {
      observeAcknowledgement = false;
      acknowledgementQueued.resolve();
    }
    return queued;
  };
  publisher.write = async (next) => {
    if (hold) {
      hold = false;
      writeStarted.resolve();
      await releaseWrite.promise;
    }
    await originalWrite(next);
  };
  const blocker = publisher.publish({ stage: (base) => base });
  await writeStarted.promise;
  const abort = new AbortController();
  try {
    observeAcknowledgement = true;
    const waiting = runtime.manage({ action: "wait", bash_id: bashId }, abort.signal);
    await acknowledgementQueued.promise;
    abort.abort();
    releaseWrite.resolve();
    await blocker;
    expect(await waiting).toMatchObject({ delivery: "pending", status: "exited", exit_code: 0 });
    await assertOwed(recordsPath, bashId);
  } finally { releaseWrite.resolve(); await runtime.shutdown(); }
});

it("keeps an acknowledgement that staged before a later abort", async () => {
  const { runtime, recordsPath, bashId, publisher } = await terminalFixture();
  const acknowledgementWriting = deferred();
  const releaseWrite = deferred();
  const originalWrite = publisher.write.bind(publisher);
  publisher.write = async (next) => {
    if (next.rows.get(bashId)?.notification_consumed_at_ms != null) {
      acknowledgementWriting.resolve();
      await releaseWrite.promise;
    }
    await originalWrite(next);
  };
  const abort = new AbortController();
  try {
    const waiting = runtime.manage({ action: "wait", bash_id: bashId }, abort.signal);
    await acknowledgementWriting.promise;
    abort.abort();
    releaseWrite.resolve();
    expect(await waiting).toMatchObject({ delivery: "result", status: "exited", exit_code: 0, output: "done" });
    const file = JSON.parse(await readFile(recordsPath, "utf8")) as BashRecordsFile;
    expect(file.records[bashId]!.notification_consumed_at_ms).toEqual(expect.any(Number));
  } finally { releaseWrite.resolve(); await runtime.shutdown(); }
});
