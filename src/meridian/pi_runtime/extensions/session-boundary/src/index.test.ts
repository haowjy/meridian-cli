import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { SessionBoundaryPublisher, writeBoundaryAtomic, type BoundaryRecord } from "../../shared/session_boundary";

describe("bounded session boundary", () => {
  it("poisons faults rather than retaining a stale quit", ({ onTestFinished }) => {
    const directory = mkdtempSync(path.join(os.tmpdir(), "boundary-"));
    onTestFinished(() => rmSync(directory, { recursive: true, force: true }));
    const file = path.join(directory, "record.json");
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
