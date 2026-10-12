import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, it } from "vitest";

import { readLogResult, readLogTail } from "./bash_log_store";

it("distinguishes unavailable output from a valid empty log while keeping UI inspection non-consuming", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "bash-log-availability-"));
  try {
    const empty = path.join(root, "empty.log");
    const missing = path.join(root, "missing.log");
    await writeFile(empty, "");
    expect(await readLogResult(empty, 64)).toEqual({ kind: "available", text: "", truncated: false });
    expect(await readLogResult(missing, 64)).toMatchObject({ kind: "missing", error: expect.stringContaining(missing) });
    expect(await readLogTail(missing, 64)).toBe("");
    expect(await readLogResult(root, 64)).toMatchObject({ kind: "unreadable", error: expect.any(String) });
    await expect(readLogTail(root, 64)).rejects.toThrow("Log unreadable");
  } finally { await rm(root, { recursive: true, force: true }); }
});

it("bounds byte tails and does not emit a partial leading UTF-8 character", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "bash-log-tail-"));
  try {
    const log = path.join(root, "utf8.log");
    await writeFile(log, "prefix🙂tail");
    expect(await readLogResult(log, 6)).toEqual({ kind: "available", text: "tail", truncated: true });
  } finally { await rm(root, { recursive: true, force: true }); }
});
