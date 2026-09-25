import { closeSync, fsyncSync, mkdirSync, openSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";

export const MAX_BOUNDARY_BYTES = 16 * 1024;
export type BoundaryIdentity = { session_id: string; session_file: string };
export type BoundaryEvent = {
  type: "session_start" | "session_before_switch" | "session_shutdown";
  reason: string;
  identity?: BoundaryIdentity;
};
export type BoundaryRecord = {
  v: 1; launch_nonce: string; pid: number; revision: number;
  initial: BoundaryIdentity | null; current: BoundaryIdentity | null;
  last_event: { type: BoundaryEvent["type"]; reason: string } | null;
  quit: BoundaryIdentity | null; invalid_reason: string | null;
};
export type BoundaryCapability = { path: string; launch_nonce: string; pid: number };
const scopeKey = Symbol.for("meridian.pi.session-boundary.v1");
const scope = globalThis as typeof globalThis & {
  [scopeKey]?: { capability: BoundaryCapability; publisher: SessionBoundaryPublisher };
};

function bounded(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max && !/[\u0000-\u001f]/.test(value);
}
function validIdentity(value: BoundaryIdentity | undefined): value is BoundaryIdentity {
  return !!value && bounded(value.session_id, 256) && bounded(value.session_file, 4096) && path.isAbsolute(value.session_file);
}
export function reduceBoundary(record: BoundaryRecord, event: BoundaryEvent): BoundaryRecord {
  if (record.invalid_reason) return record;
  if (!bounded(event.reason, 64) || record.revision >= Number.MAX_SAFE_INTEGER) {
    throw new Error("Invalid Pi lifecycle event");
  }
  if (event.type === "session_start" && record.last_event?.type === "session_start" &&
      record.last_event.reason === event.reason && event.identity &&
      record.current?.session_id === event.identity.session_id &&
      record.current?.session_file === event.identity.session_file) return record;
  const next = { ...record, revision: record.revision + 1,
    last_event: { type: event.type, reason: event.reason }, quit: null };
  if (event.type === "session_before_switch") return next;
  if (!validIdentity(event.identity)) throw new Error("Invalid Pi native identity");
  if (event.type === "session_start") {
    return { ...next, initial: record.initial ?? event.identity, current: event.identity };
  }
  return { ...next, quit: event.reason === "quit" ? event.identity : null };
}

export function readBoundaryCapability(env: NodeJS.ProcessEnv = process.env): BoundaryCapability | null {
  const file = env._MERIDIAN_PI_SESSION_BOUNDARY_PATH;
  const nonce = env._MERIDIAN_PI_SESSION_BOUNDARY_NONCE;
  delete env._MERIDIAN_PI_SESSION_BOUNDARY_PATH;
  delete env._MERIDIAN_PI_SESSION_BOUNDARY_NONCE;
  if (!file && !nonce) return scope[scopeKey]?.capability ?? null;
  if (!file || !path.isAbsolute(file) || !bounded(nonce, 256)) throw new Error("Invalid Pi boundary capability");
  return { path: file, launch_nonce: nonce, pid: process.pid };
}
export function publisherFor(capability: BoundaryCapability): SessionBoundaryPublisher {
  const prior = scope[scopeKey];
  if (prior) {
    if (JSON.stringify(prior.capability) !== JSON.stringify(capability)) {
      prior.publisher.poison("capability_mismatch");
      throw new Error("Pi boundary capability changed");
    }
    return prior.publisher;
  }
  const publisher = new SessionBoundaryPublisher(capability);
  scope[scopeKey] = { capability, publisher };
  return publisher;
}
export class SessionBoundaryPublisher {
  private record: BoundaryRecord;
  private poisoned = false;
  constructor(private readonly capability: BoundaryCapability,
    private readonly publish = writeBoundaryAtomic) {
    this.record = { v: 1, launch_nonce: capability.launch_nonce, pid: capability.pid,
      revision: 0, initial: null, current: null, last_event: null, quit: null, invalid_reason: null };
  }
  observe(event: BoundaryEvent): void {
    if (this.poisoned) throw new Error("Pi boundary publisher poisoned");
    try {
      const next = reduceBoundary(this.record, event);
      if (next === this.record) return;
      this.record = next;
      this.publish(this.capability.path, this.record);
    } catch (error) {
      this.poison("observer_fault");
      throw error;
    }
  }
  poison(reason: string): void {
    this.poisoned = true;
    this.record = { ...this.record, revision: this.record.revision + 1,
      invalid_reason: reason.slice(0, 64), quit: null };
    try { writeBoundaryAtomic(this.capability.path, this.record); }
    catch {
      // A stale quit must not survive a failed publication.
      try { unlinkSync(this.capability.path); } catch { /* missing or unwritable */ }
    }
  }
}
export function writeBoundaryAtomic(file: string, record: BoundaryRecord): void {
  const encoded = JSON.stringify(record) + "\n";
  if (Buffer.byteLength(encoded) > MAX_BOUNDARY_BYTES) throw new Error("Pi boundary size limit");
  mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temp = file + ".tmp-" + process.pid + "-" + Math.random().toString(16).slice(2);
  const fd = openSync(temp, "wx", 0o600);
  try { writeFileSync(fd, encoded); fsyncSync(fd); } finally { closeSync(fd); }
  renameSync(temp, file);
}
