import { mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { MAX_BOUNDARY_BYTES, publisherFor, reduceBoundary, writeBoundaryAtomic, type BoundaryCapability, type BoundaryRecord } from "../../shared/session_boundary";
import { registerSessionBoundaryHooks } from "./index";

const tempDirs: string[] = [];
function fixture(): { capability: BoundaryCapability; record: BoundaryRecord } {
  const dir = mkdtempSync(path.join(os.tmpdir(), "pi-session-boundary-"));
  tempDirs.push(dir);
  const capability: BoundaryCapability = {
    path: path.join(dir, "state.json"), run_id: "run-test", attempt_id: "attempt-test",
    transport_scope_id: "scope-test", launch_nonce: `nonce-${dir}`, pid: process.pid,
  };
  return {
    capability,
    record: { v: 1, observer_version: 1, run_id: capability.run_id, attempt_id: capability.attempt_id,
      transport_scope_id: capability.transport_scope_id, launch_nonce: capability.launch_nonce,
      pid: capability.pid, revision: 0, phase: "ready", native: null, invalid_reason: null },
  };
}
afterEach(() => { for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true }); });

describe("Pi session-boundary observer", () => {
  it("reduces quit to a duplicate-idempotent candidate, then makes contradictions sticky", () => {
    const { record } = fixture();
    const a = { session_id: "native-a", session_file: "/tmp/a.jsonl" };
    const candidate = reduceBoundary(record, { type: "session_shutdown", reason: "quit", identity: a });
    expect(candidate.phase).toBe("quit_candidate");
    expect(reduceBoundary(candidate, { type: "session_shutdown", reason: "quit", identity: a })).toBe(candidate);
    const invalid = reduceBoundary(candidate, { type: "session_start" });
    expect(invalid.phase).toBe("invalid");
    expect(reduceBoundary(invalid, { type: "session_shutdown", reason: "quit", identity: a })).toBe(invalid);
  });

  it("allows exactly the first native startup event but invalidates a later replacement", () => {
    const { capability } = fixture();
    const publisher = publisherFor(capability);
    publisher.initialize();
    publisher.observe({ type: "session_start" });
    expect((JSON.parse(readFileSync(capability.path, "utf8")) as BoundaryRecord).phase).toBe("ready");
    publisher.observe({ type: "session_start" });
    expect((JSON.parse(readFileSync(capability.path, "utf8")) as BoundaryRecord).phase).toBe("invalid");
  });

  it("persists state and hooks sample actual quit context; re-registration retains process state", () => {
    const { capability } = fixture();
    const publisher = publisherFor(capability);
    publisher.initialize();
    const handlers = new Map<string, (...args: unknown[]) => unknown>();
    const fakePi = { on: (name: string, handler: (...args: unknown[]) => unknown) => handlers.set(name, handler) };
    registerSessionBoundaryHooks(fakePi as never, publisher);
    registerSessionBoundaryHooks(fakePi as never, publisherFor(capability));
    handlers.get("session_shutdown")?.({ reason: "quit" }, { sessionManager: {
      getSessionId: () => "native-a", getSessionFile: () => "/tmp/a.jsonl",
    } });
    const record = JSON.parse(readFileSync(capability.path, "utf8")) as BoundaryRecord;
    expect(record.phase).toBe("quit_candidate");
    expect(record.native).toEqual({ session_id: "native-a", session_file: "/tmp/a.jsonl" });
    handlers.get("session_start")?.();
    expect((JSON.parse(readFileSync(capability.path, "utf8")) as BoundaryRecord).phase).toBe("invalid");
  });

  it("rejects publication failure after candidate and stays poisoned", () => {
    const { capability } = fixture();
    const publisher = publisherFor(capability);
    publisher.initialize();
    publisher.observe({ type: "session_shutdown", reason: "quit", identity: { session_id: "a", session_file: "/tmp/a.jsonl" } });
    // A nonempty directory at the destination forces atomic rename to fail after the
    // previous durable candidate has been moved out of the way for inspection.
    const priorPath = `${capability.path}.prior`;
    renameSync(capability.path, priorPath);
    mkdirSync(capability.path);
    writeFileSync(path.join(capability.path, "blocker"), "x");
    expect(() => publisher.observe({ type: "session_start" })).toThrow();
    expect(() => publisher.observe({ type: "session_shutdown", reason: "quit", identity: { session_id: "a", session_file: "/tmp/a.jsonl" } })).toThrow(/poisoned/);
    expect(readFileSync(priorPath, "utf8")).toContain("quit_candidate");
  });

  it("bounds records and rejects oversized native identity", () => {
    const { record } = fixture();
    const tooLong = "x".repeat(MAX_BOUNDARY_BYTES);
    const { capability } = fixture();
    const publisher = publisherFor(capability);
    expect(() => publisher.initialize()).not.toThrow();
    publisher.observe({ type: "session_shutdown", reason: "quit", identity: { session_id: tooLong, session_file: "/tmp/a" } });
    expect((JSON.parse(readFileSync(capability.path, "utf8")) as BoundaryRecord).phase).toBe("invalid");
    expect(() => writeBoundaryAtomic(capability.path, { ...record, run_id: "x".repeat(MAX_BOUNDARY_BYTES) })).toThrow(/Invalid Pi session-boundary record/);
  });
});
