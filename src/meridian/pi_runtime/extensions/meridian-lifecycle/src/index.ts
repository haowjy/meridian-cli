import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";

import type { ExtensionAPI } from "../../types";

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
};

type NotificationState = {
  id: string;
  queuedAtMs: number;
  delivered: boolean;
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
const CLI_UNAVAILABLE_BACKOFF_MS = 30_000;
const MAX_TEXT_SNIPPETS = 96;
const MAX_TEXT_DEPTH = 5;
const WRAPPER_LOG_TAIL_BYTES = 64 * 1024;

function nowMs(): number {
  return Date.now();
}

function makeId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function parentSpawnIdFromEnv(): string | null {
  const raw =
    process.env.MERIDIAN_PARENT_SPAWN_ID?.trim() ||
    process.env.MERIDIAN_SPAWN_ID?.trim() ||
    "";
  return raw.length > 0 ? raw : null;
}

function emitRaw(event: Record<string, unknown>): void {
  try {
    process.stdout.write(`${JSON.stringify(event)}\n`);
  } catch {
    // best effort only
  }
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

function isFailedSubspawnOutcome(event: InternalSubspawnEvent): boolean {
  if (event.success === false) {
    return true;
  }
  const status = normalizedStatus(event.status);
  return status === "failed" || status === "cancelled";
}

function failureReasonFromInternalEvent(event: InternalSubspawnEvent): string | null {
  if (typeof event.reason === "string" && event.reason.trim().length > 0) {
    return event.reason.trim();
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
  const session = {
    sessionId: makeId("session"),
    parentSpawnId: parentSpawnIdFromEnv(),
    trackedChildren: new Map<string, ChildState>(),
    meridianSpawnWrapperJobs: new Set<string>(),
    knownMeridianSpawnIds: new Set<string>(),
    pendingNotification: null as NotificationState | null,
    parentIdle: false,
    awaitingNotificationCompletion: false,
    hadTrackedSinceNotificationCycle: false,
    pendingFailureCount: 0,
    lastFailureSubspawnId: null as string | null,
    lastFailureReason: null as string | null,
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

  const maybeQueueNotification = (): void => {
    if (!session.parentIdle) {
      return;
    }
    if (session.pendingNotification) {
      return;
    }

    const tracked = trackedCount();
    if (session.pendingFailureCount > 0) {
      const notificationId = makeId("n");
      const failureCount = session.pendingFailureCount;
      const failedSubspawnId = session.lastFailureSubspawnId;
      const failureReason = session.lastFailureReason;
      session.pendingFailureCount = 0;
      session.lastFailureSubspawnId = null;
      session.lastFailureReason = null;
      session.hadTrackedSinceNotificationCycle = tracked > 0;
      session.pendingNotification = {
        id: notificationId,
        queuedAtMs: nowMs(),
        delivered: false,
      };

      emitRaw({
        type: "meridian.notification.queued",
        ...envelope(notificationId),
        notification_id: notificationId,
        reason: "child_failed",
        tracked_count: tracked,
        failure_count: failureCount,
        failed_subspawn_id: failedSubspawnId,
        failure_reason: failureReason,
      });

      try {
        pi.sendMessage(
          {
            customType: "meridian-lifecycle",
            content:
              "A background child task failed. Check failure details and decide recovery while remaining work may continue.",
            display: true,
            details: {
              kind: "child_failed",
              tracked_count: tracked,
              failure_count: failureCount,
              failed_subspawn_id: failedSubspawnId,
              failure_reason: failureReason,
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
      }
      return;
    }

    if (tracked > 0) {
      return;
    }
    if (!session.hadTrackedSinceNotificationCycle) {
      emitQuiescenceReady();
      return;
    }

    const notificationId = makeId("n");
    session.hadTrackedSinceNotificationCycle = false;
    session.pendingNotification = {
      id: notificationId,
      queuedAtMs: nowMs(),
      delivered: false,
    };

    emitRaw({
      type: "meridian.notification.queued",
      ...envelope(notificationId),
      notification_id: notificationId,
      reason: "children_drained",
      tracked_count: 0,
    });

    try {
      pi.sendMessage(
        {
          customType: "meridian-lifecycle",
          content:
            "Background work completed. Summarize status and decide next action or finalize.",
          display: true,
          details: {
            kind: "children_drained",
            tracked_count: 0,
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
    }
  };

  const addTrackedChild = (childId: string, waitPolicy: WaitPolicy, kind: "bash" | "meridian_spawn"): void => {
    session.trackedChildren.set(childId, {
      waitPolicy,
      kind,
      startedAtMs: nowMs(),
    });
    if (waitPolicy === "tracked") {
      session.hadTrackedSinceNotificationCycle = true;
    }
  };

  const maybeRemoveChild = (childId: string): void => {
    if (!session.trackedChildren.has(childId)) {
      return;
    }
    session.trackedChildren.delete(childId);
    maybeQueueNotification();
  };

  const noteTrackedChildFailure = (event: InternalSubspawnEvent): void => {
    const childId = event.subspawn_id;
    if (!childId) {
      return;
    }
    const childState = session.trackedChildren.get(childId);
    if (!childState || childState.waitPolicy !== "tracked") {
      return;
    }
    if (!isFailedSubspawnOutcome(event)) {
      return;
    }

    session.pendingFailureCount += 1;
    session.lastFailureSubspawnId = childId;
    session.lastFailureReason = failureReasonFromInternalEvent(event);
  };

  const registerMeridianSpawnId = (spawnId: string): void => {
    if (!isMeridianSpawnId(spawnId)) {
      return;
    }
    if (session.knownMeridianSpawnIds.has(spawnId)) {
      return;
    }

    session.knownMeridianSpawnIds.add(spawnId);
    emitCanonicalSubspawnStart(spawnId, "meridian_spawn", "tracked");
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

  const handleObservedMeridianSpawnOutput = (event: ToolResultEvent, associatedToMeridianSpawn: boolean): void => {
    if (!associatedToMeridianSpawn) {
      return;
    }
    const spawnIds = extractMeridianSpawnIds(event);
    for (const spawnId of spawnIds) {
      registerMeridianSpawnId(spawnId);
    }
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
        noteTrackedChildFailure(event);

        const subspawnId = event.subspawn_id;
        const isMeridianWrapper =
          session.meridianSpawnWrapperJobs.has(subspawnId) ||
          (kindFromInternalEvent(event) === "meridian_spawn" && !isMeridianSpawnId(subspawnId));

        if (isMeridianWrapper) {
          const childSpawnIds = await discoverMeridianSpawnIdsFromWrapperEnd(event);
          for (const spawnId of childSpawnIds) {
            registerMeridianSpawnId(spawnId);
          }
          if (childSpawnIds.length > 0) {
            ensureChildStatusPoller();
          }
        }

        maybeRemoveChild(subspawnId);
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
    unsubscribeInternalSubspawnStart();
    unsubscribeInternalSubspawnEnd();
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
          if (commandLooksLikeMeridianSpawn) {
            session.meridianSpawnWrapperJobs.add(jobId);
          }
        }
      }

      if (
        commandLooksLikeMeridianSpawn ||
        (resultJobId != null && session.meridianSpawnWrapperJobs.has(resultJobId))
      ) {
        handleObservedMeridianSpawnOutput(event, true);
        ensureChildStatusPoller();
      }

      if (event.details?.state === "exited") {
        const jobId = resultJobId;
        if (jobId) {
          maybeRemoveChild(jobId);
          session.meridianSpawnWrapperJobs.delete(jobId);
        }
      }
      return;
    }

    if (toolName === "bash_bg_wait" || toolName === "bash_bg_kill") {
      const jobId = resultJobId;
      const meridianAssociated = !!jobId && session.meridianSpawnWrapperJobs.has(jobId);
      handleObservedMeridianSpawnOutput(event, meridianAssociated);
      if (jobId && event.details?.found !== false) {
        const status = event.details?.job?.status;
        if (status && status !== "running") {
          maybeRemoveChild(jobId);
          session.meridianSpawnWrapperJobs.delete(jobId);
        }
      }
      ensureChildStatusPoller();
      return;
    }

    if (toolName === "bash_bg_read") {
      const jobId = resultJobId;
      const meridianAssociated = !!jobId && session.meridianSpawnWrapperJobs.has(jobId);
      handleObservedMeridianSpawnOutput(event, meridianAssociated);
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
          maybeRemoveChild(jobId);
          session.meridianSpawnWrapperJobs.delete(jobId);
        }
      }
      handleObservedMeridianSpawnOutput(event, hasMeridianWrapper);
      ensureChildStatusPoller();
    }
  });
}
