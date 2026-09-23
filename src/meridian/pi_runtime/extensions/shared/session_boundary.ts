import { closeSync, fsyncSync, mkdirSync, openSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";

export const MAX_BOUNDARY_BYTES = 16 * 1024;
const MAX_LABEL = 256;
const MAX_NATIVE_VALUE = 4096;

export type BoundaryIdentity = {
  session_id: string;
  session_file: string;
};

export type BoundaryRecord = {
  v: 1;
  observer_version: 1;
  run_id: string;
  attempt_id: string;
  transport_scope_id: string;
  launch_nonce: string;
  pid: number;
  revision: number;
  phase: "ready" | "quit_candidate" | "invalid";
  native: BoundaryIdentity | null;
  invalid_reason: string | null;
};

export type BoundaryCapability = Pick<BoundaryRecord,
  "run_id" | "attempt_id" | "transport_scope_id" | "launch_nonce" | "pid"> & { path: string };

export type BoundaryEvent =
  | { type: "session_start" }
  | { type: "session_shutdown"; reason: string; identity?: BoundaryIdentity }
  | { type: "session_before_switch" }
  | { type: "reload" };

const publisherRegistrySymbol = Symbol.for("meridian.pi.session-boundary.publishers.v1");
const capabilitySymbol = Symbol.for("meridian.pi.session-boundary.capability.v1");
type PublisherRegistry = Map<string, SessionBoundaryPublisher>;
const processPublishers = ((globalThis as typeof globalThis & { [publisherRegistrySymbol]?: PublisherRegistry })[publisherRegistrySymbol] ??= new Map());
const processScope = globalThis as typeof globalThis & { [capabilitySymbol]?: BoundaryCapability };

export function publisherFor(capability: BoundaryCapability): SessionBoundaryPublisher {
  const key = capability.launch_nonce;
  let publisher = processPublishers.get(key);
  if (!publisher) {
    publisher = new SessionBoundaryPublisher(capability);
    processPublishers.set(key, publisher);
  } else if (!publisher.matches(capability)) {
    publisher.invalidate("capability_mismatch");
  }
  return publisher;
}

export function reduceBoundary(record: BoundaryRecord, event: BoundaryEvent): BoundaryRecord {
  if (record.phase === "invalid") return record;
  if (record.phase === "quit_candidate") {
    if (event.type === "session_shutdown" && event.reason === "quit" && sameIdentity(record.native, event.identity)) {
      return record;
    }
    return invalidRecord(record, "lifecycle_contradiction");
  }
  if (event.type === "session_start" || event.type === "session_before_switch" || event.type === "reload") {
    return invalidRecord(record, "lifecycle_changed");
  }
  if (event.type !== "session_shutdown") return record;
  if (event.reason !== "quit") return invalidRecord(record, "non_quit_shutdown");
  if (!validIdentity(event.identity)) return invalidRecord(record, "invalid_native_identity");
  return { ...record, revision: incrementRevision(record.revision), phase: "quit_candidate", native: event.identity, invalid_reason: null };
}

export class SessionBoundaryPublisher {
  private record: BoundaryRecord;
  private poisoned = false;
  private initialized = false;
  private initialSessionStartSeen = false;

  constructor(private readonly capability: BoundaryCapability) {
    this.record = {
      v: 1, observer_version: 1,
      run_id: capability.run_id, attempt_id: capability.attempt_id,
      transport_scope_id: capability.transport_scope_id, launch_nonce: capability.launch_nonce,
      pid: capability.pid, revision: 0, phase: "ready", native: null, invalid_reason: null,
    };
  }

  matches(capability: BoundaryCapability): boolean {
    return capability.path === this.capability.path && capability.launch_nonce === this.capability.launch_nonce &&
      capability.run_id === this.capability.run_id && capability.attempt_id === this.capability.attempt_id &&
      capability.transport_scope_id === this.capability.transport_scope_id && capability.pid === this.capability.pid;
  }

  initialize(): void {
    if (this.initialized) return;
    this.write(this.record);
    this.initialized = true;
  }

  observe(event: BoundaryEvent): void {
    if (this.poisoned) throw new Error("Pi session-boundary publisher is poisoned");
    if (event.type === "session_start" && this.record.phase === "ready" && !this.initialSessionStartSeen) {
      this.initialSessionStartSeen = true;
      return;
    }
    const next = reduceBoundary(this.record, event);
    if (next === this.record) return;
    this.record = next;
    this.write(next);
  }

  invalidate(reason: string): void {
    if (this.poisoned) throw new Error("Pi session-boundary publisher is poisoned");
    if (this.record.phase === "invalid") return;
    this.record = invalidRecord(this.record, reason);
    this.write(this.record);
  }

  poison(reason: string): void {
    this.poisoned = true;
    this.record = invalidRecord(this.record, reason);
    try { writeBoundaryAtomic(this.capability.path, this.record); } catch { /* preserve the original hook failure for Pi */ }
  }

  private write(record: BoundaryRecord): void {
    try {
      writeBoundaryAtomic(this.capability.path, record);
    } catch (error) {
      this.poisoned = true;
      this.record = invalidRecord(this.record, "publication_failed");
      throw error;
    }
  }
}

export function readBoundaryCapability(env: NodeJS.ProcessEnv = process.env): BoundaryCapability | null {
  const pathValue = env._MERIDIAN_PI_SESSION_BOUNDARY_PATH;
  if (!pathValue) return processScope[capabilitySymbol] ?? null;
  const capability: BoundaryCapability = {
    path: pathValue,
    run_id: env._MERIDIAN_PI_SESSION_BOUNDARY_RUN_ID ?? "",
    attempt_id: env._MERIDIAN_PI_SESSION_BOUNDARY_ATTEMPT_ID ?? "",
    transport_scope_id: env._MERIDIAN_PI_SESSION_BOUNDARY_SCOPE_ID ?? "",
    launch_nonce: env._MERIDIAN_PI_SESSION_BOUNDARY_NONCE ?? "",
    pid: Number(env._MERIDIAN_PI_SESSION_BOUNDARY_PID),
  };
  if (!path.isAbsolute(capability.path) || !isBounded(capability.run_id, MAX_LABEL) || !isBounded(capability.attempt_id, MAX_LABEL) ||
      !isBounded(capability.transport_scope_id, MAX_LABEL) || !isBounded(capability.launch_nonce, MAX_LABEL) ||
      !Number.isSafeInteger(capability.pid) || capability.pid <= 0 || capability.pid !== process.pid) {
    throw new Error("Invalid Pi session-boundary launch capability");
  }
  for (const key of ["_MERIDIAN_PI_SESSION_BOUNDARY_PATH", "_MERIDIAN_PI_SESSION_BOUNDARY_RUN_ID", "_MERIDIAN_PI_SESSION_BOUNDARY_ATTEMPT_ID", "_MERIDIAN_PI_SESSION_BOUNDARY_SCOPE_ID", "_MERIDIAN_PI_SESSION_BOUNDARY_NONCE", "_MERIDIAN_PI_SESSION_BOUNDARY_PID"]) delete env[key];
  processScope[capabilitySymbol] = capability;
  return capability;
}

export function writeBoundaryAtomic(filePath: string, record: BoundaryRecord): void {
  validateRecord(record);
  const encoded = `${JSON.stringify(record)}\n`;
  if (Buffer.byteLength(encoded, "utf8") > MAX_BOUNDARY_BYTES) throw new Error("Pi session-boundary record exceeds size limit");
  const directory = path.dirname(filePath);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const tempPath = `${filePath}.tmp-${process.pid}-${Math.random().toString(16).slice(2)}`;
  let fd: number | undefined;
  try {
    fd = openSync(tempPath, "wx", 0o600);
    writeFileSync(fd, encoded, "utf8");
    fsyncSync(fd);
    closeSync(fd); fd = undefined;
    renameSync(tempPath, filePath);
    const dirFd = openSync(directory, "r");
    try { fsyncSync(dirFd); } finally { closeSync(dirFd); }
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

function validateRecord(record: BoundaryRecord): void {
  if (record.v !== 1 || record.observer_version !== 1 || !["ready", "quit_candidate", "invalid"].includes(record.phase) ||
      !isBounded(record.run_id, MAX_LABEL) || !isBounded(record.attempt_id, MAX_LABEL) || !isBounded(record.transport_scope_id, MAX_LABEL) ||
      !isBounded(record.launch_nonce, MAX_LABEL) || !Number.isSafeInteger(record.pid) || record.pid <= 0 ||
      !Number.isSafeInteger(record.revision) || record.revision < 0 || record.revision > 2 ||
      (record.phase === "quit_candidate" && !validIdentity(record.native)) ||
      (record.phase !== "quit_candidate" && record.native !== null) ||
      (record.phase === "invalid" ? !isBounded(record.invalid_reason, MAX_LABEL) : record.invalid_reason !== null)) {
    throw new Error("Invalid Pi session-boundary record");
  }
}

function validIdentity(value: BoundaryIdentity | undefined | null): value is BoundaryIdentity {
  return !!value && isBounded(value.session_id, MAX_NATIVE_VALUE) && isBounded(value.session_file, MAX_NATIVE_VALUE) && path.isAbsolute(value.session_file);
}
function sameIdentity(a: BoundaryIdentity | null, b: BoundaryIdentity | undefined): boolean {
  return !!a && !!b && a.session_id === b.session_id && a.session_file === b.session_file;
}
function isBounded(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max && !/[\u0000-\u001f]/.test(value);
}
function incrementRevision(value: number): number {
  if (value >= 2) throw new Error("Pi session-boundary revision overflow");
  return value + 1;
}
function invalidRecord(record: BoundaryRecord, reason: string): BoundaryRecord {
  return { ...record, revision: Math.min(record.revision + 1, 2), phase: "invalid", native: null, invalid_reason: reason.slice(0, MAX_LABEL) };
}
