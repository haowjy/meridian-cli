import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";

import {
  extractMeridianSpawnIdsFromText,
  isMeridianSpawnCommand,
  isMeridianSpawnId,
  MERIDIAN_SPAWN_ID_PATTERN,
} from "../../shared/meridian_spawn";
import type {
  ChildOutcome,
  CommandResult,
  InternalSubspawnEvent,
  ToolContentPart,
  ToolResultEvent,
  WaitPolicy,
} from "./lifecycle_types";

export const ROLE = process.env.MERIDIAN_PI_SESSION_ROLE === "spawned" ? "spawned" : "primary";
export const INTERNAL_SUBSPAWN_START_EVENT = "meridian:subspawn:start";
export const INTERNAL_SUBSPAWN_END_EVENT = "meridian:subspawn:end";
export const TERMINAL_MERIDIAN_STATUSES = new Set(["succeeded", "failed", "cancelled"]);
export const CHILD_STATUS_POLL_INTERVAL_MS = 2_500;
export const CHILD_STATUS_POLL_TIMEOUT_MS = 8_000;
export const CHILD_SPAWN_CANCEL_TIMEOUT_MS = 8_000;
export const CLI_UNAVAILABLE_BACKOFF_MS = 30_000;
export const MAX_TEXT_SNIPPETS = 96;
export const MAX_TEXT_DEPTH = 5;
export const WRAPPER_LOG_TAIL_BYTES = 64 * 1024;
export const DEFAULT_CHILD_WAVE_TIMEOUT_MS = 300_000;
export const MIN_CHILD_WAVE_TIMEOUT_MS = 1;
export const MAX_CHILD_WAVE_TIMEOUT_MS = 60 * 60 * 1_000;
export const DEFAULT_WAVE_KILL_GRACE_MS = 2_000;
export const MAX_WAVE_KILL_GRACE_MS = 30_000;
export const MAX_WAVE_NOTIFICATION_OUTCOME_COUNT = 12;
export const MAX_WAVE_NOTIFICATION_REASON_CHARS = 72;
export const MAX_WAVE_NOTIFICATION_SUMMARY_CHARS = 384;

export function nowMs(): number {
  return Date.now();
}

export function makeId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export function parseEnvInteger(name: string): number | null {
  const raw = process.env[name];
  if (typeof raw !== "string") {
    return null;
  }
  const trimmed = raw.trim();
  if (trimmed.length === 0) {
    return null;
  }
  const parsed = Number.parseInt(trimmed, 10);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  return parsed;
}

export function truncateText(value: string, maxChars: number): string {
  if (maxChars <= 0) {
    return "";
  }
  if (value.length <= maxChars) {
    return value;
  }
  if (maxChars === 1) {
    return "…";
  }
  return `${value.slice(0, maxChars - 1)}…`;
}

export function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

export function resolveChildWaveTimeoutMs(): number {
  const explicitMs = parseEnvInteger("MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS");
  if (explicitMs != null) {
    return clamp(explicitMs, MIN_CHILD_WAVE_TIMEOUT_MS, MAX_CHILD_WAVE_TIMEOUT_MS);
  }

  const explicitSeconds = parseEnvInteger("MERIDIAN_PI_CHILD_WAVE_TIMEOUT_SECONDS");
  if (explicitSeconds != null) {
    return clamp(
      explicitSeconds * 1_000,
      MIN_CHILD_WAVE_TIMEOUT_MS,
      MAX_CHILD_WAVE_TIMEOUT_MS,
    );
  }

  return DEFAULT_CHILD_WAVE_TIMEOUT_MS;
}

export function resolveWaveKillGraceMs(): number {
  const explicit = parseEnvInteger("MERIDIAN_PI_CHILD_WAVE_KILL_GRACE_MS");
  if (explicit != null) {
    return clamp(explicit, 100, MAX_WAVE_KILL_GRACE_MS);
  }
  return DEFAULT_WAVE_KILL_GRACE_MS;
}

export function parentSpawnIdFromEnv(): string | null {
  const raw =
    process.env.MERIDIAN_PARENT_SPAWN_ID?.trim() ||
    process.env.MERIDIAN_SPAWN_ID?.trim() ||
    "";
  return raw.length > 0 ? raw : null;
}

export function waitPolicyFrom(event: ToolResultEvent): WaitPolicy {
  if (event.details?.wait_policy === "detached") {
    return "detached";
  }
  if (event.input?.wait_policy === "detached") {
    return "detached";
  }
  if (event.details?.job?.wait_policy === "detached") {
    return "detached";
  }
  return "tracked";
}

export function jobIdFrom(event: ToolResultEvent): string | null {
  return (
    (event.details as { task_id?: string })?.task_id ||
    event.details?.job_id ||
    (event.details?.job as { task_id?: string })?.task_id ||
    event.details?.job?.job_id ||
    (event.input as { task_id?: string })?.task_id ||
    event.input?.job_id ||
    null
  );
}

export function persistentFromEvent(event: ToolResultEvent): boolean {
  if (event.details?.persistent === true) {
    return true;
  }
  return event.details?.job?.persistent === true;
}

export function parseInternalSubspawnEvent(data: unknown): InternalSubspawnEvent | null {
  if (!data || typeof data !== "object") {
    return null;
  }
  return data as InternalSubspawnEvent;
}

export function kindFromInternalEvent(event: InternalSubspawnEvent): import("./lifecycle_types").SubspawnKind {
  if (event.kind === "meridian_spawn") {
    return "meridian_spawn";
  }
  if (event.command_is_meridian_spawn === true) {
    return "meridian_spawn";
  }
  const command = typeof event.command === "string" ? event.command : "";
  return isMeridianSpawnCommand(command) ? "meridian_spawn" : "bash";
}

export function normalizedStatus(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const status = value.trim().toLowerCase();
  return status.length > 0 ? status : null;
}

export function intFromUnknown(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  if (typeof value === "string" && value.trim().length > 0) {
    const parsed = Number.parseInt(value.trim(), 10);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return null;
}

export function stringFromUnknown(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

export function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export function sendSignalBestEffort(pid: number, signal: NodeJS.Signals): void {
  try {
    process.kill(-pid, signal);
    return;
  } catch {
    // fall through
  }
  try {
    process.kill(pid, signal);
  } catch {
    // ignore
  }
}

export async function cancelTrackedPid(pid: number, killGraceMs: number): Promise<void> {
  if (!Number.isInteger(pid) || pid <= 0) {
    return;
  }

  sendSignalBestEffort(pid, "SIGTERM");
  if (!isProcessAlive(pid)) {
    return;
  }
  await new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, Math.max(1, killGraceMs));
    timer.unref?.();
  });
  if (isProcessAlive(pid)) {
    sendSignalBestEffort(pid, "SIGKILL");
  }
}

export function failureReasonFromInternalEvent(event: InternalSubspawnEvent): string | null {
  if (typeof event.reason === "string" && event.reason.trim().length > 0) {
    return event.reason.trim();
  }
  const exitCode = intFromUnknown(event.exit_code);
  if (exitCode != null && exitCode !== 0) {
    return `exit_code_${exitCode}`;
  }
  const signal = stringFromUnknown(event.signal);
  if (signal) {
    return `signal_${signal}`;
  }
  const status = normalizedStatus(event.status);
  if (status === "failed" || status === "cancelled") {
    return status;
  }
  if (event.success === false) {
    return "failed";
  }
  return null;
}

export function outcomeFromTerminalEvent(event: InternalSubspawnEvent): ChildOutcome | null {
  const subspawnId = event.subspawn_id;
  if (!subspawnId) {
    return null;
  }

  const status = normalizedStatus(event.status);
  if (status === "cancelled") {
    return {
      subspawn_id: subspawnId,
      status: "cancelled",
      success: false,
      reason: failureReasonFromInternalEvent(event) ?? "cancelled",
    };
  }
  if (status === "failed") {
    return {
      subspawn_id: subspawnId,
      status: "failed",
      success: false,
      reason: failureReasonFromInternalEvent(event) ?? "failed",
    };
  }
  if (status === "succeeded") {
    return {
      subspawn_id: subspawnId,
      status: "succeeded",
      success: true,
    };
  }
  if (event.success === true) {
    return {
      subspawn_id: subspawnId,
      status: "succeeded",
      success: true,
    };
  }
  if (event.success === false) {
    return {
      subspawn_id: subspawnId,
      status: "failed",
      success: false,
      reason: failureReasonFromInternalEvent(event) ?? "failed",
    };
  }
  return null;
}

export function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value != null;
}

export { isMeridianSpawnCommand, isMeridianSpawnId };

export function collectTextSnippets(value: unknown, sink: string[], depth = 0): void {
  if (sink.length >= MAX_TEXT_SNIPPETS || depth > MAX_TEXT_DEPTH) {
    return;
  }
  if (typeof value === "string") {
    if (value.length > 0) {
      sink.push(value);
    }
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      if (sink.length >= MAX_TEXT_SNIPPETS) {
        return;
      }
      collectTextSnippets(item, sink, depth + 1);
    }
    return;
  }
  if (!isObjectRecord(value)) {
    return;
  }

  for (const [key, nested] of Object.entries(value)) {
    if (sink.length >= MAX_TEXT_SNIPPETS) {
      return;
    }
    if (
      key === "command" ||
      key === "customType" ||
      key === "toolName" ||
      key === "name"
    ) {
      continue;
    }
    collectTextSnippets(nested, sink, depth + 1);
  }
}

export function commandFromEvent(event: ToolResultEvent): string {
  const detailsCommand = event.details?.command;
  const jobCommand = event.details?.job?.command;
  const inputCommand = event.input?.command;
  const command =
    (typeof inputCommand === "string" && inputCommand) ||
    (typeof detailsCommand === "string" && detailsCommand) ||
    (typeof jobCommand === "string" && jobCommand) ||
    "";
  return command;
}

export function extractMeridianSpawnIds(event: ToolResultEvent): string[] {
  const snippets: string[] = [];
  collectTextSnippets(event.content, snippets);
  collectTextSnippets(event.details, snippets);

  const ids = new Set<string>();
  for (const snippet of snippets) {
    MERIDIAN_SPAWN_ID_PATTERN.lastIndex = 0;
    for (const match of snippet.matchAll(MERIDIAN_SPAWN_ID_PATTERN)) {
      const id = match[0];
      if (isMeridianSpawnId(id)) {
        ids.add(id);
      }
    }
  }

  return [...ids];
}

export { extractMeridianSpawnIdsFromText };

export async function readTailFromPath(filePath: string, maxBytes: number): Promise<string> {
  if (filePath.trim().length === 0) {
    return "";
  }

  try {
    const stat = await fs.stat(filePath);
    if (stat.size <= 0) {
      return "";
    }

    const bytesToRead = Math.min(Math.max(1, maxBytes), stat.size);
    const start = stat.size - bytesToRead;
    const fd = await fs.open(filePath, "r");
    try {
      const buffer = Buffer.allocUnsafe(bytesToRead);
      const { bytesRead } = await fd.read(buffer, 0, bytesToRead, start);
      return buffer.subarray(0, bytesRead).toString("utf-8");
    } finally {
      await fd.close();
    }
  } catch {
    return "";
  }
}

export function parseStatusFromOutput(text: string): string | null {
  const trimmed = text.trim();
  if (trimmed.length === 0) {
    return null;
  }

  const tryExtractFromObject = (value: unknown): string | null => {
    if (!isObjectRecord(value)) {
      return null;
    }
    const raw = value.status;
    return typeof raw === "string" ? raw.trim().toLowerCase() : null;
  };

  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (Array.isArray(parsed)) {
      for (const item of parsed) {
        const status = tryExtractFromObject(item);
        if (status) {
          return status;
        }
      }
    } else {
      const status = tryExtractFromObject(parsed);
      if (status) {
        return status;
      }
    }
  } catch {
    // not pure json output
  }

  const lines = trimmed.split(/\r?\n/).map((line) => line.trim());
  for (const line of lines) {
    if (!line.startsWith("{")) {
      continue;
    }
    try {
      const parsed = JSON.parse(line) as unknown;
      const status = tryExtractFromObject(parsed);
      if (status) {
        return status;
      }
    } catch {
      // ignore malformed lines
    }
  }

  const statusMatch = trimmed.match(/\bstatus\s*[:=]\s*(running|queued|starting|finalizing|succeeded|failed|cancelled)\b/i);
  if (statusMatch?.[1]) {
    return statusMatch[1].toLowerCase();
  }

  return null;
}

export async function runCommand(command: string, args: string[], timeoutMs: number): Promise<CommandResult> {
  return await new Promise<CommandResult>((resolve) => {
    let stdout = "";
    let stderr = "";
    let finished = false;
    let timedOut = false;

    const child = spawn(command, args, {
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
      detached: false,
    });

    const finalize = (result: CommandResult): void => {
      if (finished) {
        return;
      }
      finished = true;
      resolve(result);
    };

    const timeout = setTimeout(() => {
      timedOut = true;
      try {
        child.kill("SIGTERM");
      } catch {
        // ignore
      }
    }, Math.max(1, timeoutMs));

    child.stdout?.setEncoding("utf-8");
    child.stdout?.on("data", (chunk: string | Buffer) => {
      stdout += typeof chunk === "string" ? chunk : chunk.toString("utf-8");
    });

    child.stderr?.setEncoding("utf-8");
    child.stderr?.on("data", (chunk: string | Buffer) => {
      stderr += typeof chunk === "string" ? chunk : chunk.toString("utf-8");
    });

    child.once("error", (error) => {
      clearTimeout(timeout);
      finalize({
        stdout,
        stderr,
        exitCode: null,
        signal: null,
        spawnError: error instanceof Error ? error.message : String(error),
        timedOut,
      });
    });

    child.once("close", (exitCode, signal) => {
      clearTimeout(timeout);
      finalize({
        stdout,
        stderr,
        exitCode,
        signal,
        spawnError: null,
        timedOut,
      });
    });
  });
}
