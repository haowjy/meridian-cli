import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";

import type { ExtensionAPI } from "../../types";
import { createLifecycleSidecarWriter } from "../../shared/lifecycle_sidecar";

type WaitPolicy = "tracked" | "detached";
type SubspawnKind = "bash" | "meridian_spawn";

type InternalSubspawnEvent = {
  subspawn_id?: string;
  wait_policy?: WaitPolicy;
  kind?: SubspawnKind;
  command?: string;
  command_is_meridian_spawn?: boolean;
  status?: string;
  success?: boolean;
  reason?: string;
  log_path?: string;
  exit_code?: unknown;
  signal?: unknown;
  pid?: unknown;
};

type ToolContentPart = {
  type?: string;
  text?: string;
  [key: string]: unknown;
};

type ToolResultEvent = {
  toolName?: string;
  content?: ToolContentPart[];
  input?: {
    wait_policy?: WaitPolicy;
    job_id?: string;
    command?: string;
  };
  details?: {
    state?: string;
    wait_policy?: WaitPolicy;
    job_id?: string;
    pid?: number;
    command?: string;
    stdout_tail?: string;
    stderr_tail?: string;
    log_tail?: string;
    text?: string;
    message?: string;
    output?: string;
    job?: {
      job_id?: string;
      wait_policy?: WaitPolicy;
      status?: string;
      command?: string;
    };
    jobs?: Array<{
      job_id?: string;
      wait_policy?: WaitPolicy;
      status?: string;
      command?: string;
    }>;
    found?: boolean;
    [key: string]: unknown;
  };
  isError?: boolean;
  [key: string]: unknown;
};

type ChildState = {
  kind: "bash" | "meridian_spawn";
  waitPolicy: WaitPolicy;
  startedAtMs: number;
  pid: number | null;
};

type NotificationState = {
  id: string;
  queuedAtMs: number;
  delivered: boolean;
};

type ChildOutcomeStatus = "succeeded" | "failed" | "cancelled" | "timed_out";

type ChildOutcome = {
  subspawn_id: string;
  status: ChildOutcomeStatus;
  success: boolean;
  reason?: string;
};

type ActiveWaveState = {
  id: string;
  startedAtMs: number;
  deadlineAtMs: number;
  deadlineTimer: NodeJS.Timeout | null;
  trackedChildIds: Set<string>;
  outcomes: Map<string, ChildOutcome>;
};

type CommandResult = {
  stdout: string;
  stderr: string;
  exitCode: number | null;
  signal: string | null;
  spawnError: string | null;
  timedOut: boolean;
};

const ROLE = process.env.MERIDIAN_PI_SESSION_ROLE === "spawned" ? "spawned" : "primary";
const INTERNAL_SUBSPAWN_START_EVENT = "meridian:subspawn:start";
const INTERNAL_SUBSPAWN_END_EVENT = "meridian:subspawn:end";
const MERIDIAN_SPAWN_COMMAND_PATTERN = /\bmeridian\s+spawn\b/;
const MERIDIAN_SPAWN_ID_PATTERN = /\bp\d+\b/g;
const TERMINAL_MERIDIAN_STATUSES = new Set(["succeeded", "failed", "cancelled"]);
const CHILD_STATUS_POLL_INTERVAL_MS = 2_500;
const CHILD_STATUS_POLL_TIMEOUT_MS = 8_000;
const CHILD_SPAWN_CANCEL_TIMEOUT_MS = 8_000;
const CLI_UNAVAILABLE_BACKOFF_MS = 30_000;
const MAX_TEXT_SNIPPETS = 96;
const MAX_TEXT_DEPTH = 5;
const WRAPPER_LOG_TAIL_BYTES = 64 * 1024;
const DEFAULT_CHILD_WAVE_TIMEOUT_MS = 300_000;
const MIN_CHILD_WAVE_TIMEOUT_MS = 1;
const MAX_CHILD_WAVE_TIMEOUT_MS = 60 * 60 * 1_000;
const DEFAULT_WAVE_KILL_GRACE_MS = 2_000;
const MAX_WAVE_KILL_GRACE_MS = 30_000;
const MAX_WAVE_NOTIFICATION_OUTCOME_COUNT = 12;
const MAX_WAVE_NOTIFICATION_REASON_CHARS = 72;
const MAX_WAVE_NOTIFICATION_SUMMARY_CHARS = 384;
const lifecycleWriter = createLifecycleSidecarWriter(ROLE);

function nowMs(): number {
  return Date.now();
}

function makeId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function parseEnvInteger(name: string): number | null {
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

function truncateText(value: string, maxChars: number): string {
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

function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function resolveChildWaveTimeoutMs(): number {
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

function resolveWaveKillGraceMs(): number {
  const explicit = parseEnvInteger("MERIDIAN_PI_CHILD_WAVE_KILL_GRACE_MS");
  if (explicit != null) {
    return clamp(explicit, 100, MAX_WAVE_KILL_GRACE_MS);
  }
  return DEFAULT_WAVE_KILL_GRACE_MS;
}

function parentSpawnIdFromEnv(): string | null {
  const raw =
    process.env.MERIDIAN_PARENT_SPAWN_ID?.trim() ||
    process.env.MERIDIAN_SPAWN_ID?.trim() ||
    "";
  return raw.length > 0 ? raw : null;
}

function emitRaw(event: Record<string, unknown>): void {
  lifecycleWriter.append(event);
}

function waitPolicyFrom(event: ToolResultEvent): WaitPolicy {
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

function jobIdFrom(event: ToolResultEvent): string | null {
  return (
    event.details?.job_id ||
    event.details?.job?.job_id ||
    event.input?.job_id ||
    null
  );
}

function parseInternalSubspawnEvent(data: unknown): InternalSubspawnEvent | null {
  if (!data || typeof data !== "object") {
    return null;
  }
  return data as InternalSubspawnEvent;
}

function kindFromInternalEvent(event: InternalSubspawnEvent): SubspawnKind {
  if (event.kind === "meridian_spawn") {
    return "meridian_spawn";
  }
  if (event.command_is_meridian_spawn === true) {
    return "meridian_spawn";
  }
  const command = typeof event.command === "string" ? event.command : "";
  return isMeridianSpawnCommand(command) ? "meridian_spawn" : "bash";
}

function normalizedStatus(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const status = value.trim().toLowerCase();
  return status.length > 0 ? status : null;
}

function intFromUnknown(value: unknown): number | null {
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

function stringFromUnknown(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function sendSignalBestEffort(pid: number, signal: NodeJS.Signals): void {
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

async function cancelTrackedPid(pid: number, killGraceMs: number): Promise<void> {
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

function failureReasonFromInternalEvent(event: InternalSubspawnEvent): string | null {
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

function outcomeFromTerminalEvent(event: InternalSubspawnEvent): ChildOutcome | null {
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

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value != null;
}

function isMeridianSpawnCommand(command: string): boolean {
  return MERIDIAN_SPAWN_COMMAND_PATTERN.test(command);
}

function isMeridianSpawnId(value: string): boolean {
  return /^p\d+$/.test(value);
}

function collectTextSnippets(value: unknown, sink: string[], depth = 0): void {
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

function commandFromEvent(event: ToolResultEvent): string {
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

function extractMeridianSpawnIds(event: ToolResultEvent): string[] {
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

function extractMeridianSpawnIdsFromText(text: string): string[] {
  if (text.length === 0) {
    return [];
  }

  const ids = new Set<string>();
  MERIDIAN_SPAWN_ID_PATTERN.lastIndex = 0;
  for (const match of text.matchAll(MERIDIAN_SPAWN_ID_PATTERN)) {
    const id = match[0];
    if (isMeridianSpawnId(id)) {
      ids.add(id);
    }
  }
  return [...ids];
}

async function readTailFromPath(filePath: string, maxBytes: number): Promise<string> {
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

function parseStatusFromOutput(text: string): string | null {
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

async function runCommand(command: string, args: string[], timeoutMs: number): Promise<CommandResult> {
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

export default function meridianLifecycleExtension(pi: ExtensionAPI): void {
  const childWaveTimeoutMs = resolveChildWaveTimeoutMs();
  const childWaveKillGraceMs = resolveWaveKillGraceMs();

  const session = {
    sessionId: makeId("session"),
    parentSpawnId: parentSpawnIdFromEnv(),
    trackedChildren: new Map<string, ChildState>(),
    meridianSpawnWrapperJobs: new Set<string>(),
    knownMeridianSpawnIds: new Set<string>(),
    activeWave: null as ActiveWaveState | null,
    pendingNotification: null as NotificationState | null,
    parentIdle: false,
    awaitingNotificationCompletion: false,
    childStatusPollTimer: null as NodeJS.Timeout | null,
    childStatusPollInFlight: false,
    cliUnavailableUntilMs: 0,
    shuttingDown: false,
  };

  const envelope = (correlationId: string) => ({
    schema_version: 1,
    session_id: session.sessionId,
    parent_spawn_id: session.parentSpawnId,
    correlation_id: correlationId,
    emitted_at_ms: nowMs(),
  });

  const trackedCount = (): number => {
    let count = 0;
    for (const child of session.trackedChildren.values()) {
      if (child.waitPolicy === "tracked") {
        count += 1;
      }
    }
    return count;
  };

  const emitQuiescenceReady = (): void => {
    emitRaw({
      type: "meridian.quiescence.ready",
      ...envelope(makeId("q")),
      role: ROLE,
      tracked_count: trackedCount(),
      pending_notification_count: session.pendingNotification ? 1 : 0,
    });
  };

  const emitCanonicalSubspawnStart = (subspawnId: string, kind: SubspawnKind, waitPolicy: WaitPolicy): void => {
    const payload = {
      type: "meridian.subspawn.start",
      ...envelope(subspawnId),
      kind,
      subspawn_id: subspawnId,
      wait_policy: waitPolicy,
    };
    emitRaw(payload);
    pi.events.emit(INTERNAL_SUBSPAWN_START_EVENT, payload);
  };

  const emitCanonicalSubspawnEnd = (
    subspawnId: string,
    kind: SubspawnKind,
    outcome: { status?: string; success?: boolean; reason?: string },
  ): void => {
    const payload = {
      type: "meridian.subspawn.end",
      ...envelope(subspawnId),
      kind,
      subspawn_id: subspawnId,
      status: outcome.status,
      success: outcome.success,
      reason: outcome.reason,
    };
    emitRaw(payload);
    pi.events.emit(INTERNAL_SUBSPAWN_END_EVENT, payload);
  };

  const trackedMeridianSpawnIds = (): string[] => {
    const ids: string[] = [];
    for (const [childId, child] of session.trackedChildren.entries()) {
      if (child.kind !== "meridian_spawn") {
        continue;
      }
      if (!isMeridianSpawnId(childId)) {
        continue;
      }
      ids.push(childId);
    }
    return ids;
  };

  const stopChildStatusPoller = (): void => {
    if (session.childStatusPollTimer == null) {
      return;
    }
    clearInterval(session.childStatusPollTimer);
    session.childStatusPollTimer = null;
  };

  const clearWaveTimer = (wave: ActiveWaveState | null): void => {
    if (!wave?.deadlineTimer) {
      return;
    }
    clearTimeout(wave.deadlineTimer);
    wave.deadlineTimer = null;
  };

  const clearActiveWave = (): void => {
    if (session.activeWave == null) {
      return;
    }
    clearWaveTimer(session.activeWave);
    session.activeWave = null;
  };

  const trackedChildIds = (): string[] => {
    const ids: string[] = [];
    for (const [childId, child] of session.trackedChildren.entries()) {
      if (child.waitPolicy === "tracked") {
        ids.push(childId);
      }
    }
    return ids;
  };

  const attachChildToActiveWave = (childId: string): void => {
    if (!session.parentIdle) {
      return;
    }
    if (session.activeWave == null) {
      return;
    }
    if (!session.trackedChildren.has(childId)) {
      return;
    }
    session.activeWave.trackedChildIds.add(childId);
  };

  const buildWaveOutcomes = (wave: ActiveWaveState): ChildOutcome[] => {
    const outcomes = [...wave.outcomes.values()];
    outcomes.sort((left, right) => left.subspawn_id.localeCompare(right.subspawn_id));
    return outcomes;
  };

  const formatWaveOutcomeSummary = (outcome: ChildOutcome): string => {
    const base = `${outcome.subspawn_id} ${outcome.status}`;
    if (typeof outcome.reason !== "string" || outcome.reason.trim().length === 0) {
      return base;
    }
    const normalizedReason = normalizeWhitespace(outcome.reason);
    if (normalizedReason.length === 0) {
      return base;
    }
    const shortReason = truncateText(normalizedReason, MAX_WAVE_NOTIFICATION_REASON_CHARS);
    return `${base} (${shortReason})`;
  };

  const buildWaveNotificationContent = (childOutcomes: ChildOutcome[]): string => {
    const childCount = childOutcomes.length;
    if (childCount === 0) {
      return "Background work completed. 0 children finished.";
    }

    const cappedOutcomes = childOutcomes
      .slice(0, MAX_WAVE_NOTIFICATION_OUTCOME_COUNT)
      .map(formatWaveOutcomeSummary);
    const remainingCount = childCount - cappedOutcomes.length;
    if (remainingCount > 0) {
      cappedOutcomes.push(`+${remainingCount} more`);
    }

    const summary = truncateText(
      cappedOutcomes.join("; "),
      MAX_WAVE_NOTIFICATION_SUMMARY_CHARS,
    );
    return `Background work completed. ${childCount} children finished: ${summary}.`;
  };

  const sendWaveNotification = (waveReason: "children_drained" | "wave_deadline", trackedForPayload: number): void => {
    const wave = session.activeWave;
    if (wave == null) {
      return;
    }
    const notificationId = makeId("n");
    const childOutcomes = buildWaveOutcomes(wave);
    const hadFailures = childOutcomes.some((outcome) => outcome.success === false);
    const hadTimeouts = childOutcomes.some((outcome) => outcome.status === "timed_out");

    session.pendingNotification = {
      id: notificationId,
      queuedAtMs: nowMs(),
      delivered: false,
    };

    emitRaw({
      type: "meridian.notification.queued",
      ...envelope(notificationId),
      notification_id: notificationId,
      reason: waveReason,
      tracked_count: trackedForPayload,
    });

    try {
      pi.sendMessage(
        {
          customType: "meridian-lifecycle",
          content: buildWaveNotificationContent(childOutcomes),
          display: true,
          details: {
            kind: "wave_completed",
            tracked_count: trackedForPayload,
            child_outcomes: childOutcomes,
            had_failures: hadFailures,
            had_timeouts: hadTimeouts,
            wave_reason: waveReason,
          },
        },
        {
          deliverAs: "followUp",
          triggerTurn: true,
        },
      );

      session.pendingNotification.delivered = true;
      session.awaitingNotificationCompletion = true;
      emitRaw({
        type: "meridian.notification.delivered",
        ...envelope(notificationId),
        notification_id: notificationId,
        deliver_as: "followUp",
        trigger_turn: true,
      });
    } catch (error) {
      emitRaw({
        type: "meridian.notification.failed",
        ...envelope(notificationId),
        notification_id: notificationId,
        reason: "sendMessage_error",
        error: error instanceof Error ? error.message : String(error),
      });
      session.pendingNotification = null;
      session.awaitingNotificationCompletion = false;
    } finally {
      clearActiveWave();
    }
  };

  const maybeCompleteWaveByDrain = (): void => {
    if (session.activeWave == null) {
      return;
    }
    if (session.activeWave.trackedChildIds.size > 0) {
      return;
    }
    sendWaveNotification("children_drained", 0);
  };

  const startWave = (): void => {
    if (!session.parentIdle || session.activeWave != null || session.pendingNotification != null) {
      return;
    }
    const ids = trackedChildIds();
    if (ids.length === 0) {
      return;
    }

    const waveId = makeId("w");
    const wave: ActiveWaveState = {
      id: waveId,
      startedAtMs: nowMs(),
      deadlineAtMs: nowMs() + childWaveTimeoutMs,
      deadlineTimer: null,
      trackedChildIds: new Set(ids),
      outcomes: new Map<string, ChildOutcome>(),
    };
    wave.deadlineTimer = setTimeout(() => {
      void handleWaveDeadline(waveId);
    }, childWaveTimeoutMs);
    wave.deadlineTimer.unref?.();
    session.activeWave = wave;

    emitRaw({
      type: "meridian.lifecycle.wave.started",
      ...envelope(waveId),
      wave_id: waveId,
      tracked_count: ids.length,
      timeout_ms: childWaveTimeoutMs,
      started_at_ms: wave.startedAtMs,
      deadline_at_ms: wave.deadlineAtMs,
    });
  };

  const maybeQueueNotification = (): void => {
    if (!session.parentIdle) {
      return;
    }
    if (session.pendingNotification) {
      return;
    }
    if (session.activeWave != null) {
      maybeCompleteWaveByDrain();
      return;
    }

    if (trackedCount() > 0) {
      startWave();
      return;
    }
    if (ROLE === "spawned") {
      emitQuiescenceReady();
    }
  };

  const addTrackedChild = (childId: string, waitPolicy: WaitPolicy, kind: "bash" | "meridian_spawn"): void => {
    const existing = session.trackedChildren.get(childId);
    session.trackedChildren.set(childId, {
      waitPolicy,
      kind,
      startedAtMs: nowMs(),
      pid: existing?.pid ?? null,
    });
    if (waitPolicy === "tracked") {
      attachChildToActiveWave(childId);
      maybeQueueNotification();
    }
  };

  const recordWaveOutcome = (outcome: ChildOutcome): void => {
    const wave = session.activeWave;
    if (wave == null) {
      return;
    }
    if (!wave.trackedChildIds.has(outcome.subspawn_id)) {
      return;
    }
    if (wave.outcomes.has(outcome.subspawn_id)) {
      emitRaw({
        type: "meridian.lifecycle.duplicate_child_outcome_ignored",
        ...envelope(outcome.subspawn_id),
        subspawn_id: outcome.subspawn_id,
      });
      return;
    }
    wave.outcomes.set(outcome.subspawn_id, outcome);
    wave.trackedChildIds.delete(outcome.subspawn_id);
  };

  const settleTrackedChildTerminalOutcome = (
    childId: string,
    outcome: ChildOutcome,
  ): void => {
    const childState = session.trackedChildren.get(childId);
    if (childState?.waitPolicy === "tracked") {
      recordWaveOutcome(outcome);
    }
    maybeRemoveChild(childId);
    session.meridianSpawnWrapperJobs.delete(childId);
    if (isMeridianSpawnId(childId)) {
      session.knownMeridianSpawnIds.delete(childId);
    }
    if (trackedMeridianSpawnIds().length === 0) {
      stopChildStatusPoller();
    }
  };

  const setChildPid = (childId: string, pid: number | null): void => {
    if (pid == null) {
      return;
    }
    const child = session.trackedChildren.get(childId);
    if (!child) {
      return;
    }
    child.pid = pid;
    session.trackedChildren.set(childId, child);
  };

  const dropWaveChild = (childId: string): void => {
    const wave = session.activeWave;
    if (!wave) {
      return;
    }
    wave.trackedChildIds.delete(childId);
    wave.outcomes.delete(childId);
  };

  const maybeRemoveChild = (childId: string, options?: { dropWaveChild?: boolean }): void => {
    if (!session.trackedChildren.has(childId)) {
      return;
    }
    if (options?.dropWaveChild === true) {
      dropWaveChild(childId);
    }
    session.trackedChildren.delete(childId);
    maybeQueueNotification();
  };

  const cancelTrackedMeridianSpawn = async (spawnId: string): Promise<void> => {
    const result = await runCommand(
      "meridian",
      ["spawn", "cancel", spawnId],
      CHILD_SPAWN_CANCEL_TIMEOUT_MS,
    );

    if (result.timedOut) {
      emitRaw({
        type: "meridian.lifecycle.child_cancel_failed",
        ...envelope(spawnId),
        subspawn_id: spawnId,
        reason: "cancel_timeout",
        timeout_ms: CHILD_SPAWN_CANCEL_TIMEOUT_MS,
      });
      return;
    }

    if (result.spawnError) {
      emitRaw({
        type: "meridian.lifecycle.child_cancel_failed",
        ...envelope(spawnId),
        subspawn_id: spawnId,
        reason: "cancel_spawn_error",
        error: result.spawnError,
      });
      return;
    }

    if (result.exitCode !== 0) {
      emitRaw({
        type: "meridian.lifecycle.child_cancel_failed",
        ...envelope(spawnId),
        subspawn_id: spawnId,
        reason: "cancel_nonzero_exit",
        exit_code: result.exitCode,
        signal: result.signal,
        stderr: truncateText(result.stderr, 256),
      });
    }
  };

  const handleWaveDeadline = async (waveId: string): Promise<void> => {
    if (session.shuttingDown) {
      return;
    }
    const wave = session.activeWave;
    if (!session.parentIdle || wave == null || wave.id !== waveId || session.pendingNotification != null) {
      return;
    }

    clearWaveTimer(wave);

    const timedOutChildIds = [...wave.trackedChildIds];
    const trackedCountAtDeadline = timedOutChildIds.length;
    const killTasks: Promise<void>[] = [];
    const shouldCleanupTrackedChildren = ROLE === "spawned";
    for (const childId of timedOutChildIds) {
      if (!wave.outcomes.has(childId)) {
        wave.outcomes.set(childId, {
          subspawn_id: childId,
          status: "timed_out",
          success: false,
          reason: "wave_deadline",
        });
      }
      const child = session.trackedChildren.get(childId);
      if (shouldCleanupTrackedChildren && child?.waitPolicy === "tracked") {
        if (child.pid != null) {
          killTasks.push(cancelTrackedPid(child.pid, childWaveKillGraceMs));
        }
        if (child.kind === "meridian_spawn" && isMeridianSpawnId(childId)) {
          killTasks.push(cancelTrackedMeridianSpawn(childId));
        }
      }
      maybeRemoveChild(childId);
      session.meridianSpawnWrapperJobs.delete(childId);
      if (isMeridianSpawnId(childId)) {
        session.knownMeridianSpawnIds.delete(childId);
      }
    }

    if (killTasks.length > 0) {
      await Promise.allSettled(killTasks);
    }

    wave.trackedChildIds.clear();
    sendWaveNotification("wave_deadline", trackedCountAtDeadline);
  };

  const registerMeridianSpawnId = (spawnId: string, waitPolicy: WaitPolicy): void => {
    if (!isMeridianSpawnId(spawnId)) {
      return;
    }
    addTrackedChild(spawnId, waitPolicy, "meridian_spawn");
    if (session.knownMeridianSpawnIds.has(spawnId)) {
      return;
    }

    session.knownMeridianSpawnIds.add(spawnId);
    emitCanonicalSubspawnStart(spawnId, "meridian_spawn", waitPolicy);
  };

  const discoverMeridianSpawnIdsFromWrapperEnd = async (event: InternalSubspawnEvent): Promise<string[]> => {
    const snippets: string[] = [];
    if (typeof event.reason === "string" && event.reason.length > 0) {
      snippets.push(event.reason);
    }
    if (typeof event.log_path === "string" && event.log_path.length > 0) {
      const logTail = await readTailFromPath(event.log_path, WRAPPER_LOG_TAIL_BYTES);
      if (logTail.length > 0) {
        snippets.push(logTail);
      }
    }

    const ids = new Set<string>();
    for (const snippet of snippets) {
      for (const id of extractMeridianSpawnIdsFromText(snippet)) {
        ids.add(id);
      }
    }
    return [...ids];
  };

  const handleObservedMeridianSpawnOutput = (
    event: ToolResultEvent,
    associatedToMeridianSpawn: boolean,
    sourceJobId?: string | null,
  ): string[] => {
    if (!associatedToMeridianSpawn) {
      return [];
    }
    const spawnIds = extractMeridianSpawnIds(event);
    const sourceWaitPolicy =
      sourceJobId != null && session.trackedChildren.get(sourceJobId)?.waitPolicy === "detached"
        ? "detached"
        : "tracked";
    for (const spawnId of spawnIds) {
      registerMeridianSpawnId(spawnId, sourceWaitPolicy);
    }
    if (
      spawnIds.length > 0 &&
      sourceJobId != null &&
      session.meridianSpawnWrapperJobs.has(sourceJobId)
    ) {
      maybeRemoveChild(sourceJobId, { dropWaveChild: true });
      session.meridianSpawnWrapperJobs.delete(sourceJobId);
    }
    return spawnIds;
  };

  const pollMeridianSpawnStatuses = async (): Promise<void> => {
    if (session.shuttingDown || session.childStatusPollInFlight) {
      return;
    }

    const trackedIds = trackedMeridianSpawnIds();
    if (trackedIds.length === 0) {
      stopChildStatusPoller();
      return;
    }

    const current = nowMs();
    if (current < session.cliUnavailableUntilMs) {
      return;
    }

    session.childStatusPollInFlight = true;
    try {
      for (const spawnId of trackedIds) {
        const result = await runCommand(
          "meridian",
          ["--json", "spawn", "show", spawnId, "--no-report"],
          CHILD_STATUS_POLL_TIMEOUT_MS,
        );

        if (result.spawnError && /ENOENT/i.test(result.spawnError)) {
          session.cliUnavailableUntilMs = nowMs() + CLI_UNAVAILABLE_BACKOFF_MS;
          emitRaw({
            type: "meridian.lifecycle.child_status_poll_skipped",
            ...envelope(spawnId),
            subspawn_id: spawnId,
            reason: "meridian_cli_unavailable",
            error: result.spawnError,
          });
          return;
        }

        const status =
          parseStatusFromOutput(result.stdout) ??
          parseStatusFromOutput(result.stderr);

        if (status && TERMINAL_MERIDIAN_STATUSES.has(status)) {
          const outcome = outcomeFromTerminalEvent({
            subspawn_id: spawnId,
            status,
            success: status === "succeeded",
          });
          if (outcome != null) {
            settleTrackedChildTerminalOutcome(spawnId, outcome);
          }
          emitCanonicalSubspawnEnd(spawnId, "meridian_spawn", {
            status,
            success: status === "succeeded",
          });
          continue;
        }

        if (result.timedOut) {
          emitRaw({
            type: "meridian.lifecycle.child_status_poll_timeout",
            ...envelope(spawnId),
            subspawn_id: spawnId,
          });
          continue;
        }

        if (result.exitCode !== 0 && status == null) {
          const combined = `${result.stdout}\n${result.stderr}`;
          if (/not found|unknown spawn|no such spawn/i.test(combined)) {
            const outcome = outcomeFromTerminalEvent({
              subspawn_id: spawnId,
              status: "failed",
              success: false,
              reason: "spawn_not_found",
            });
            if (outcome != null) {
              settleTrackedChildTerminalOutcome(spawnId, outcome);
            }
            emitCanonicalSubspawnEnd(spawnId, "meridian_spawn", {
              status: "failed",
              success: false,
              reason: "spawn_not_found",
            });
          }
        }
      }
    } catch (error) {
      emitRaw({
        type: "meridian.lifecycle.child_status_poll_error",
        ...envelope(makeId("poll")),
        error: error instanceof Error ? error.message : String(error),
      });
    } finally {
      session.childStatusPollInFlight = false;
    }
  };

  const ensureChildStatusPoller = (): void => {
    if (session.childStatusPollTimer != null || session.shuttingDown) {
      return;
    }
    if (trackedMeridianSpawnIds().length === 0) {
      return;
    }

    session.childStatusPollTimer = setInterval(() => {
      void pollMeridianSpawnStatuses();
    }, CHILD_STATUS_POLL_INTERVAL_MS);
    session.childStatusPollTimer.unref?.();
    void pollMeridianSpawnStatuses();
  };

  const handleAgentEnd = (): void => {
    session.parentIdle = true;
    if (session.awaitingNotificationCompletion && session.pendingNotification?.delivered) {
      emitRaw({
        type: "meridian.notification.completed",
        ...envelope(session.pendingNotification.id),
        notification_id: session.pendingNotification.id,
      });
      session.pendingNotification = null;
      session.awaitingNotificationCompletion = false;
      maybeQueueNotification();
      return;
    }

    maybeQueueNotification();
  };

  const unsubscribeInternalSubspawnStart = pi.events.on(
    INTERNAL_SUBSPAWN_START_EVENT,
    (data) => {
      const event = parseInternalSubspawnEvent(data);
      if (!event?.subspawn_id) {
        return;
      }
      const kind = kindFromInternalEvent(event);
      addTrackedChild(
        event.subspawn_id,
        event.wait_policy === "detached" ? "detached" : "tracked",
        kind,
      );
      setChildPid(event.subspawn_id, intFromUnknown(event.pid));

      if (kind === "meridian_spawn") {
        if (isMeridianSpawnId(event.subspawn_id)) {
          session.knownMeridianSpawnIds.add(event.subspawn_id);
          ensureChildStatusPoller();
        } else {
          session.meridianSpawnWrapperJobs.add(event.subspawn_id);
        }
      }
    },
  );

  const unsubscribeInternalSubspawnEnd = pi.events.on(
    INTERNAL_SUBSPAWN_END_EVENT,
    (data) => {
      const event = parseInternalSubspawnEvent(data);
      if (!event?.subspawn_id) {
        return;
      }

      void (async () => {
        const subspawnId = event.subspawn_id;
        const childState = session.trackedChildren.get(subspawnId);
        const isTracked = childState?.waitPolicy === "tracked";
        const isMeridianWrapper =
          session.meridianSpawnWrapperJobs.has(subspawnId) ||
          (kindFromInternalEvent(event) === "meridian_spawn" && !isMeridianSpawnId(subspawnId));

        let wrapperHandoffSucceeded = false;
        if (isMeridianWrapper) {
          const wrapperWaitPolicy = childState?.waitPolicy === "detached" ? "detached" : "tracked";
          const childSpawnIds = await discoverMeridianSpawnIdsFromWrapperEnd(event);
          for (const spawnId of childSpawnIds) {
            registerMeridianSpawnId(spawnId, wrapperWaitPolicy);
          }
          if (childSpawnIds.length > 0) {
            wrapperHandoffSucceeded = true;
            ensureChildStatusPoller();
          } else if (isTracked) {
            emitRaw({
              type: "meridian.lifecycle.wrapper_handoff_missing_child_id",
              ...envelope(subspawnId),
              subspawn_id: subspawnId,
            });
          }
        }

        if (isMeridianWrapper && !wrapperHandoffSucceeded && isTracked) {
          maybeQueueNotification();
          return;
        }

        const outcome = outcomeFromTerminalEvent(event);
        if (isTracked && outcome != null) {
          recordWaveOutcome(outcome);
        }

        maybeRemoveChild(subspawnId, { dropWaveChild: wrapperHandoffSucceeded });
        session.meridianSpawnWrapperJobs.delete(subspawnId);
        if (isMeridianSpawnId(subspawnId)) {
          session.knownMeridianSpawnIds.delete(subspawnId);
        }

        if (trackedMeridianSpawnIds().length === 0) {
          stopChildStatusPoller();
        }
      })();
    },
  );

  pi.on("session_start", async (_event, ctx) => {
    const sid = ctx.sessionManager.getSessionId();
    if (typeof sid === "string" && sid.trim()) {
      session.sessionId = sid;
    }
  });

  pi.on("session_shutdown", async () => {
    session.shuttingDown = true;
    stopChildStatusPoller();
    clearActiveWave();
    unsubscribeInternalSubspawnStart();
    unsubscribeInternalSubspawnEnd();
    lifecycleWriter.close();
  });

  pi.on("agent_start", async () => {
    session.parentIdle = false;
  });

  pi.on("agent_end", async () => {
    handleAgentEnd();
  });

  pi.on("tool_result", async (event: ToolResultEvent) => {
    const toolName = event.toolName ?? "";
    const command = commandFromEvent(event);
    const commandLooksLikeMeridianSpawn = isMeridianSpawnCommand(command);
    const resultJobId = jobIdFrom(event);

    if (toolName === "bash") {
      if (event.details?.state === "running") {
        const jobId = resultJobId;
        if (jobId) {
          const kind = commandLooksLikeMeridianSpawn ? "meridian_spawn" : "bash";
          addTrackedChild(jobId, waitPolicyFrom(event), kind);
          setChildPid(jobId, intFromUnknown(event.details?.pid));
          if (commandLooksLikeMeridianSpawn) {
            session.meridianSpawnWrapperJobs.add(jobId);
          }
        }
      }

      if (
        commandLooksLikeMeridianSpawn ||
        (resultJobId != null && session.meridianSpawnWrapperJobs.has(resultJobId))
      ) {
        handleObservedMeridianSpawnOutput(event, true, resultJobId);
        ensureChildStatusPoller();
      }

      if (event.details?.state === "exited") {
        const jobId = resultJobId;
        if (jobId) {
          if (!session.meridianSpawnWrapperJobs.has(jobId)) {
            maybeRemoveChild(jobId);
          }
        }
      }
      return;
    }

    if (toolName === "bash_bg_wait" || toolName === "bash_bg_kill") {
      const jobId = resultJobId;
      const meridianAssociated = !!jobId && session.meridianSpawnWrapperJobs.has(jobId);
      handleObservedMeridianSpawnOutput(event, meridianAssociated, jobId);
      if (jobId && event.details?.found !== false) {
        const status = event.details?.job?.status;
        if (status && status !== "running") {
          if (!session.meridianSpawnWrapperJobs.has(jobId)) {
            maybeRemoveChild(jobId);
          }
        }
      }
      ensureChildStatusPoller();
      return;
    }

    if (toolName === "bash_bg_read") {
      const jobId = resultJobId;
      const meridianAssociated = !!jobId && session.meridianSpawnWrapperJobs.has(jobId);
      handleObservedMeridianSpawnOutput(event, meridianAssociated, jobId);
      ensureChildStatusPoller();
      return;
    }

    if (toolName === "bash_bg_list") {
      const jobs = event.details?.jobs;
      if (!Array.isArray(jobs)) {
        return;
      }
      let hasMeridianWrapper = false;
      for (const job of jobs) {
        const jobId = job.job_id;
        if (!jobId) {
          continue;
        }
        const jobCommand = typeof job.command === "string" ? job.command : "";
        const isMeridianWrapper = isMeridianSpawnCommand(jobCommand) || session.meridianSpawnWrapperJobs.has(jobId);
        if (isMeridianWrapper) {
          hasMeridianWrapper = true;
        }
        if (job.status === "running") {
          addTrackedChild(jobId, job.wait_policy === "detached" ? "detached" : "tracked", isMeridianWrapper ? "meridian_spawn" : "bash");
          if (isMeridianWrapper) {
            session.meridianSpawnWrapperJobs.add(jobId);
          }
        } else {
          if (!session.meridianSpawnWrapperJobs.has(jobId)) {
            maybeRemoveChild(jobId);
          }
        }
      }
      handleObservedMeridianSpawnOutput(event, hasMeridianWrapper, resultJobId);
      ensureChildStatusPoller();
    }
  });
}
