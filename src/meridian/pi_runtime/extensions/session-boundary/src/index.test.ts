import { mkdtempSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { SessionBoundaryPublisher, writeBoundaryAtomic, type BoundaryRecord } from "../../shared/session_boundary";

describe("bounded session boundary", () => {
  it("keeps initial entry across switches, and only final quit qualifies", () => {
    const file = path.join(mkdtempSync(path.join(os.tmpdir(), "boundary-")), "record.json");
    const publisher = new SessionBoundaryPublisher({ path: file, launch_nonce: "nonce", pid: process.pid });
    const a = { session_id: "a", session_file: "/store/a.jsonl" };
    const b = { session_id: "b", session_file: "/store/b.jsonl" };
    publisher.observe({ type: "session_start", reason: "startup", identity: a });
    publisher.observe({ type: "session_start", reason: "startup", identity: a });
    publisher.observe({ type: "session_before_switch", reason: "resume" });
    publisher.observe({ type: "session_shutdown", reason: "resume", identity: a });
    publisher.observe({ type: "session_start", reason: "resume", identity: b });
    publisher.observe({ type: "session_shutdown", reason: "quit", identity: b });
    let record = JSON.parse(readFileSync(file, "utf8"));
    expect(record).toMatchObject({ initial: a, current: b, quit: b, revision: 6 });
    publisher.observe({ type: "session_start", reason: "reload", identity: b });
    record = JSON.parse(readFileSync(file, "utf8"));
    expect(record.quit).toBeNull();
    expect(record.initial).toEqual(a);
  });
  it("poisons faults rather than retaining a stale quit", () => {
    const file = path.join(mkdtempSync(path.join(os.tmpdir(), "boundary-")), "record.json");
    const publisher = new SessionBoundaryPublisher({ path: file, launch_nonce: "n", pid: process.pid });
    publisher.observe({ type: "session_shutdown", reason: "quit", identity: { session_id: "a", session_file: "/a" } });
    expect(() => publisher.observe({ type: "session_start", reason: "new" })).toThrow();
    expect(JSON.parse(readFileSync(file, "utf8"))).toMatchObject({ quit: null, invalid_reason: "observer_fault" });
    expect(() => publisher.observe({ type: "session_before_switch", reason: "new" })).toThrow();
  });
  it("enforces the encoded byte bound", () => {
    expect(() => writeBoundaryAtomic("/unused", { launch_nonce: "a".repeat(16384) } as BoundaryRecord)).toThrow("size limit");
  });
});
