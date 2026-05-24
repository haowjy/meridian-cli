import type { ExtensionAPI } from "../../types";
import { createLifecycleSidecarWriter } from "../../shared/lifecycle_sidecar";
import type { MeridianEventBus } from "../../shared/meridian_event_bus";
import { extractMeridianSpawnIdsFromText, isMeridianSpawnId } from "../../shared/meridian_spawn";
import type {
  ActiveWaveState,
  ChildOutcome,
  ChildState,
  InternalSubspawnEvent,
  NotificationState,
  SubspawnKind,
  ToolResultEvent,
  WaitPolicy,
} from "./lifecycle_types";
import {
  CHILD_SPAWN_CANCEL_TIMEOUT_MS,
  CHILD_STATUS_POLL_INTERVAL_MS,
  CHILD_STATUS_POLL_TIMEOUT_MS,
  CLI_UNAVAILABLE_BACKOFF_MS,
  INTERNAL_SUBSPAWN_END_EVENT,
  INTERNAL_SUBSPAWN_START_EVENT,
  MAX_WAVE_NOTIFICATION_OUTCOME_COUNT,
  MAX_WAVE_NOTIFICATION_REASON_CHARS,
  MAX_WAVE_NOTIFICATION_SUMMARY_CHARS,
  ROLE,
  TERMINAL_MERIDIAN_STATUSES,
  WRAPPER_LOG_TAIL_BYTES,
  cancelTrackedPid,
  intFromUnknown,
  kindFromInternalEvent,
  makeId,
  nowMs,
  normalizeWhitespace,
  outcomeFromTerminalEvent,
  parentSpawnIdFromEnv,
  parseInternalSubspawnEvent,
  parseStatusFromOutput,
  readTailFromPath,
  resolveChildWaveTimeoutMs,
  resolveWaveKillGraceMs,
  runCommand,
  truncateText,
  extractMeridianSpawnIds,
} from "./lifecycle_utils";

const lifecycleWriter = createLifecycleSidecarWriter(ROLE);

function emitRaw(event: Record<string, unknown>): void {
  lifecycleWriter.append(event);
}

export type LifecycleChildTracker = {
  registerBusListeners: () => void;
  registerPiHooks: () => void;
  setToolResultHandler: (handler: (event: ToolResultEvent) => void) => void;
  session: {
    trackedChildren: Map<string, ChildState>;
    meridianSpawnWrapperJobs: Set<string>;
  };
  addTrackedChild: (
    childId: string,
    waitPolicy: WaitPolicy,
    kind: "bash" | "meridian_spawn",
    persistent?: boolean,
  ) => void;
  setChildPid: (childId: string, pid: number | null) => void;
  maybeRemoveChild: (childId: string, options?: { dropWaveChild?: boolean }) => void;
  handleObservedMeridianSpawnOutput: (
    event: ToolResultEvent,
    associatedToMeridianSpawn: boolean,
    sourceJobId?: string | null,
  ) => string[];
  ensureChildStatusPoller: () => void;
};

export function createLifecycleChildTracker(
  pi: ExtensionAPI,
  bus: MeridianEventBus,
): LifecycleChildTracker {
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
      if (child.waitPolicy === "tracked" && !child.persistent) {
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
    bus.emit(INTERNAL_SUBSPAWN_START_EVENT, payload);
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
    bus.emit(INTERNAL_SUBSPAWN_END_EVENT, payload);
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
      if (child.waitPolicy === "tracked" && !child.persistent) {
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

  const addTrackedChild = (
    childId: string,
    waitPolicy: WaitPolicy,
    kind: "bash" | "meridian_spawn",
    persistent = false,
  ): void => {
    const existing = session.trackedChildren.get(childId);
    session.trackedChildren.set(childId, {
      waitPolicy,
      kind,
      persistent,
      startedAtMs: existing?.startedAtMs ?? nowMs(),
      pid: existing?.pid ?? null,
    });
    if (waitPolicy === "tracked" && !persistent) {
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
    if (childState?.waitPolicy === "tracked" && !childState.persistent) {
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
      if (shouldCleanupTrackedChildren && child?.waitPolicy === "tracked" && !child.persistent) {
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

  let unsubscribeInternalSubspawnStart = (): void => undefined;
  let unsubscribeInternalSubspawnEnd = (): void => undefined;

  const registerBusListeners = (): void => {
    unsubscribeInternalSubspawnStart();
    unsubscribeInternalSubspawnEnd();
    unsubscribeInternalSubspawnStart = bus.on(
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
        event.persistent === true,
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

    unsubscribeInternalSubspawnEnd = bus.on(
    INTERNAL_SUBSPAWN_END_EVENT,
    (data) => {
      const event = parseInternalSubspawnEvent(data);
      if (!event?.subspawn_id) {
        return;
      }

      void (async () => {
        const subspawnId = event.subspawn_id;
        const childState = session.trackedChildren.get(subspawnId);
        const isTracked = childState?.waitPolicy === "tracked" && !childState.persistent;
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
          const failureOutcome: ChildOutcome = {
            subspawn_id: subspawnId,
            status: "failed",
            success: false,
            reason: "wrapper_handoff_missing_child_id",
          };
          recordWaveOutcome(failureOutcome);
          emitCanonicalSubspawnEnd(subspawnId, "meridian_spawn", {
            status: "failed",
            success: false,
            reason: "wrapper_handoff_missing_child_id",
          });
          settleTrackedChildTerminalOutcome(subspawnId, failureOutcome);
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
  };

  let toolResultHandler: (event: ToolResultEvent) => void = () => undefined;

  const registerPiHooks = (): void => {
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
      toolResultHandler(event);
    });
  };

  return {
    registerBusListeners,
    registerPiHooks,
    handleToolResult: (event: ToolResultEvent) => {
      toolResultHandler(event);
    },
    setToolResultHandler: (handler: (event: ToolResultEvent) => void) => {
      toolResultHandler = handler;
    },
    session,
    addTrackedChild,
    setChildPid,
    maybeRemoveChild,
    handleObservedMeridianSpawnOutput,
    ensureChildStatusPoller,
  };
}
