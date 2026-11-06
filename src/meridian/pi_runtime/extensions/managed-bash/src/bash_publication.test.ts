import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { BashRecord, BashRecordsFile } from "../../shared/schemas";
import { BashPublication, changeRow, withoutRows, withRow, type Published } from "./bash_publication";

const row = (id: string, patch: Partial<BashRecord> = {}): BashRecord => ({
  bash_id: id, command: "true", cwd: "/", pid: null, status: "running", is_background: false, is_tracked: true,
  exit_code: null, started_at_ms: 1, ended_at_ms: null, log_path: "l", stdout_log_path: "o", stderr_log_path: "e",
  log_bytes: 0, timeout_min: 1, ...patch,
});

let root: string;
let recordsPath: string;
beforeEach(async () => { root = await mkdtemp(path.join(tmpdir(), "pi-publication-unit-")); recordsPath = path.join(root, "bash-records.json"); });
afterEach(async () => { await rm(root, { recursive: true, force: true }); });

const open = (evidence: (published: Published) => Published = (published) => published) =>
  new BashPublication({ path: recordsPath, spawnId: "p-unit", evidence });
const disk = async (): Promise<BashRecordsFile> => JSON.parse(await readFile(recordsPath, "utf8")) as BashRecordsFile;
/** A directory where the file belongs makes the atomic rename fail for real. */
const breakStorage = async (): Promise<void> => { await mkdir(recordsPath); };
const fixStorage = (): Promise<void> => rm(recordsPath, { recursive: true, force: true });

/** An arbitrary extra async hop around a caller's await, as any future caller may add. */
async function through<T>(depth: number, promise: Promise<T>): Promise<T> {
  return depth === 0 ? promise : through(depth - 1, (async () => await promise)());
}

describe("BashPublication ordering contract", () => {
  it.each([0, 1, 2, 5, 9])("a task-fatal failure is claimed before a queued successor stages, behind %i caller wrappers", async (depth) => {
    const publication = open();
    await breakStorage();
    let claimed = false;
    let claimedWhenSuccessorStaged: boolean | undefined;

    const failing = through(depth, publication.publish({
      stage: (base) => withRow(base, row("b-a")),
      fatal: () => { claimed = true; },
    }));
    const failureSeenByCaller = failing.catch(() => "failed");
    // Queued synchronously behind the failing slot: its stage runs the instant the tail releases.
    const successor = publication.publish({ stage: () => { claimedWhenSuccessorStaged = claimed; return null; } });

    expect(await failureSeenByCaller).toBe("failed");
    expect(await successor).toBe(false);
    expect(claimedWhenSuccessorStaged).toBe(true);
    expect(publication.current.rows.size).toBe(0);
  });

  it("claims exactly once per failure, and only for commands that declare the policy", async () => {
    const publication = open();
    await breakStorage();
    const claims: unknown[] = [];

    await expect(publication.publish({ stage: (base) => withRow(base, row("b-a")) })).rejects.toThrow(); // caller-recoverable
    expect(claims).toEqual([]);
    await expect(publication.publish({ stage: (base) => withRow(base, row("b-a")), fatal: (error) => claims.push(error) })).rejects.toThrow();
    expect(claims).toHaveLength(1);
    // A declined stage is not a failure.
    expect(await publication.publish({ stage: () => null, fatal: (error) => claims.push(error) })).toBe(false);
    expect(claims).toHaveLength(1);
  });

  it("a stage that throws is a failure of its slot, not an escape from it", async () => {
    const publication = open();
    let claimed = false;
    await expect(publication.publish({ stage: () => { throw new Error("stage bug"); }, fatal: () => { claimed = true; } })).rejects.toThrow("stage bug");
    expect(claimed).toBe(true);
    expect(await publication.publish({ stage: (base) => withRow(base, row("b-a")) })).toBe(true);
  });
});

describe("BashPublication adoption", () => {
  it("preserves a present empty runtime diagnostic instead of writing a different snapshot", async () => {
    const publication = open();
    await publication.publish({ stage: (base) => ({ ...base, runtimeError: "" }) });
    expect((await disk()).runtime_error).toBe("");
    expect(publication.current.runtimeError).toBe("");
  });

  it("adopts exactly what was written, only after the write, and runs effects in the same slot", async () => {
    const events: string[] = [];
    const publication = open();
    const slots = [
      publication.publish({ stage: (base) => { events.push("stage-1"); return withRow(base, row("b-a")); }, adopted: () => events.push("adopted-1") }),
      publication.publish({ stage: (base) => { events.push(`stage-2:${[...base.rows.keys()]}`); return changeRow("b-a", () => ({ is_tracked: false }))(base); }, adopted: () => events.push("adopted-2") }),
    ];
    expect(await Promise.all(slots)).toEqual([true, true]);
    // Each slot stages against its predecessor's adopted value; nothing overlaps.
    expect(events).toEqual(["stage-1", "adopted-1", "stage-2:b-a", "adopted-2"]);
    expect(publication.current.rows.get("b-a")?.is_tracked).toBe(false);
    expect((await disk()).records["b-a"]).toEqual(publication.current.rows.get("b-a"));
  });

  it("a failed write adopts nothing and leaves the next slot staging against the previous value", async () => {
    const publication = open();
    await publication.publish({ stage: (base) => withRow(base, row("b-a")) });
    await fixStorage().then(breakStorage);
    let adopted = false;
    await expect(publication.publish({ stage: changeRow("b-a", () => ({ is_tracked: false })), adopted: () => { adopted = true; } })).rejects.toThrow();
    expect(adopted).toBe(false);
    expect(publication.current.rows.get("b-a")?.is_tracked).toBe(true);

    await fixStorage();
    let base: Published | undefined;
    await publication.publish({ stage: (published) => { base = published; return null; } });
    expect(base?.rows.get("b-a")?.is_tracked).toBe(true);
  });

  it("declining writes nothing; a change to an absent row declines", async () => {
    const publication = open();
    expect(await publication.publish({ stage: changeRow("b-missing", () => ({ is_tracked: false })) })).toBe(false);
    expect(await publication.publish({ stage: () => null })).toBe(false);
    await expect(readFile(recordsPath, "utf8")).rejects.toThrow("ENOENT");
  });

  it("merges live evidence into every slot's base, and the file carries what the slot adopted", async () => {
    let progress = 0;
    const publication = open((published) => ({
      ...published, rows: new Map([...published.rows].map(([id, value]) => [id, { ...value, log_bytes: progress }])),
    }));
    await publication.publish({ stage: (base) => withRow(base, row("b-a")) });
    progress = 42; // live progress between lifecycle writes: never written by itself
    expect((await disk()).records["b-a"]?.log_bytes).toBe(0);
    await publication.publish({ stage: changeRow("b-a", () => ({ is_background: true })) });
    expect((await disk()).records["b-a"]).toMatchObject({ is_background: true, log_bytes: 42 });
    expect(publication.current.rows.get("b-a")?.log_bytes).toBe(42);
  });

  it("removal derives from the base like any other change", async () => {
    const publication = open();
    await publication.publish({ stage: (base) => withRow(withRow(base, row("b-a")), row("b-b")) });
    await publication.publish({ stage: (base) => withoutRows(base, ["b-a"]) });
    expect(Object.keys((await disk()).records)).toEqual(["b-b"]);
  });
});
