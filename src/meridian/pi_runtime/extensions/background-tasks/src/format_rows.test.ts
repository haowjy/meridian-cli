import { describe, expect, it } from "vitest";

import { findPsRow, formatPsRow, isLiveSpawnRow, isLiveTaskRow } from "./format_rows";
import type { BackgroundTaskRecord, PsRow } from "./types";

const baseTask: BackgroundTaskRecord = {
  task_id: "t1",
  label: "test",
  command: "echo hi",
  cwd: "/tmp",
  pid: 42,
  wait_policy: "tracked",
  status: "running",
  success: null,
  exit_code: null,
  signal: null,
  started_at_ms: 1,
  ended_at_ms: null,
  stdout_log_path: "/a/out",
  stderr_log_path: "/a/err",
  combined_log_path: "/a/combined",
  log_bytes: 0,
  log_truncated: false,
};

describe("format_rows", () => {
  it("formats spawn and task rows", () => {
    const spawn: PsRow = { kind: "meridian_spawn", spawn_id: "p1", status: "running" };
    expect(formatPsRow(spawn)).toContain("[spawn] p1");
    expect(formatPsRow({ kind: "process", ...baseTask })).toContain("[task] t1");
  });

  it("finds rows by id", () => {
    const rows: PsRow[] = [
      { kind: "process", ...baseTask },
      { kind: "meridian_spawn", spawn_id: "p9", status: "queued" },
    ];
    expect(findPsRow(rows, "p9")?.kind).toBe("meridian_spawn");
    expect(findPsRow(rows, "t1")?.kind).toBe("process");
  });

  it("detects live rows", () => {
    expect(isLiveTaskRow({ kind: "process", ...baseTask })).toBe(true);
    expect(isLiveTaskRow({ kind: "process", ...baseTask, status: "exited" })).toBe(false);
    expect(isLiveSpawnRow({ kind: "meridian_spawn", spawn_id: "p1", status: "running" })).toBe(true);
    expect(isLiveSpawnRow({ kind: "meridian_spawn", spawn_id: "p1", status: "succeeded" })).toBe(false);
  });
});
