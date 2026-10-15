import { parseRef } from "../../shared/ids";
import { existsSync, mkdirSync, watch, type FSWatcher } from "node:fs";
import { open, readdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";

import type { ExtensionAPI, Theme } from "@earendil-works/pi-coding-agent";

import { writeJsonAtomic } from "../../shared/json_file";
import { runMeridianCommand } from "../../shared/meridian_cli";
import { admitDelivery, admittedWorkIds, readDeliveryReceipts, readPrivateJson, readSpawnObservations, reservedSpawnIds } from "../../shared/delivery_receipts";
import {
  currentSpawnIdFromEnv,
  currentRunIdFromEnv,
  resolveBashRecordsPath,
  resolveClearedSpawnsPath,
  resolveDeliveryFaultPath,
  resolveDeliveryReceiptsPath,
  resolveObservedSpawnsPath,
  resolveRunScopeDir,
  resolveSpawnScopeDir,
  resolveSpawnsDir,
} from "../../shared/pi_state_paths";
import type { BashRecordsFile, SpawnStateFile } from "../../shared/schemas";
import { isTerminalBashStatus, parseBashRecordsFile, parseSpawnStateFile } from "../../shared/schemas";
import { openLogOverlay } from "../../shared/log_overlay";
import { bashOutcome } from "../../shared/task_outcome";
import {
  openTaskPanel,
  hasTaskPanelUI,
  showTaskText,
  type PanelCommandContext,
  type SelectablePanelColumn,
} from "../../shared/selectable_panel";
import { formatDurationSecs, renderTable } from "../../shared/ui";

type ChildDiscovery = { rows: SpawnStateFile[]; revision: number };
type NotificationItem = {
  id: string;
  kind: "spawn" | "bash";
  status: string;
  label: string;
  duration: string;
  exitCode?: number;
  error?: string;
};
type NotificationFrame = {
  prefix: string;
  detail: string;
  truncationNotice: string;
  lookup: string;
};
type NotificationCandidate = { item: NotificationItem; mandatoryBytes: number };
type ClearedSpawnsFile = { v: 1; spawn_id: string; updated_at_ms: number; cleared_spawn_ids: string[] };
const POLL_MS = 500;
const FULL_RECONCILE_MS = 30_000;
const MAX_REPORT_BYTES = 64 * 1024;
const MAX_NOTIFICATION_BYTES = 64 * 1024;
const MAX_NOTIFICATION_MEMBERS = 32;
const NOTIFICATION_SEPARATOR = "\n\n";

/** Launch-scoped owner: durable rows are obligations; queue claims are only mechanics. */
export class SpawnWatchRuntime {
  private readonly currentSpawnId = currentSpawnIdFromEnv();
  private readonly currentRunId = currentRunIdFromEnv();
  private readonly spawnsDir = resolveSpawnsDir();
  private readonly spawnScopeDir = resolveSpawnScopeDir(this.currentSpawnId);
  private readonly runScopeDir = resolveRunScopeDir(this.currentRunId);
  private readonly bashRecordsPath = resolveBashRecordsPath(this.currentRunId);
  private readonly clearedPath = resolveClearedSpawnsPath(this.currentSpawnId);
  private readonly receiptsPath = resolveDeliveryReceiptsPath(this.currentRunId);
  private readonly observationsPath = resolveObservedSpawnsPath(this.currentSpawnId);
  private readonly deliveryFaultPath = resolveDeliveryFaultPath(this.currentRunId);
  private readonly queued = new Map<string, string[]>();
  private candidateChildIds = new Set<string>();
  private watchers: FSWatcher[] = [];
  private polling: NodeJS.Timeout | null = null;
  private reconciliation: NodeJS.Timeout | null = null;
  private running = false;
  private hostGeneration = 0;
  private discoveryRevision = 1;
  private reconciledRevision = 0;
  private drainTail: Promise<void> = Promise.resolve();
  private idleRequest: Promise<void> | null = null;

  constructor(private pi: ExtensionAPI, private isIdle: () => boolean = () => false) {}
  rebind(pi: ExtensionAPI): void {
    this.hostGeneration++;
    this.discoveryRevision++;
    this.pi = pi;
    this.isIdle = () => false;
  }
  observeActive(check: () => boolean): void {
    this.hostGeneration++;
    this.isIdle = check;
  }
  observeIdle(check: () => boolean): void {
    this.isIdle = check;
    this.requestIdleDelivery();
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    mkdirSync(this.spawnsDir, { recursive: true });
    mkdirSync(this.runScopeDir, { recursive: true });
    mkdirSync(this.spawnScopeDir, { recursive: true });
    const watched: Array<[string, () => void]> = [
      [this.spawnsDir, () => { this.discoveryRevision++; this.requestIdleDelivery(); }],
      [this.runScopeDir, () => this.requestIdleDelivery()],
    ];
    if (this.currentRunId !== this.currentSpawnId) {
      watched.push([this.spawnScopeDir, () => this.requestIdleDelivery()]);
    }
    for (const [dir, onChange] of watched) {
      try {
        const watcher = watch(dir, { recursive: false }, onChange);
        watcher.on("error", (error) => { this.logWarning(error); watcher.close(); });
        watcher.unref(); this.watchers.push(watcher);
      } catch (error) { this.logWarning(error); }
    }
    // Ordinary idle ticks reread only known candidates and parent-local files.
    this.polling = setInterval(() => this.requestIdleDelivery(), POLL_MS);
    this.polling.unref();
    // The root watcher can miss events, including state publication after a new
    // directory event. A slow full reconciliation bounds that discovery delay.
    this.reconciliation = setInterval(() => {
      this.discoveryRevision++;
      this.requestIdleDelivery();
    }, FULL_RECONCILE_MS);
    this.reconciliation.unref();
    this.requestIdleDelivery();
  }

  stop(): void {
    this.running = false;
    this.hostGeneration++;
    this.isIdle = () => false;
    for (const watcher of this.watchers) watcher.close();
    this.watchers = [];
    if (this.polling) clearInterval(this.polling);
    if (this.reconciliation) clearInterval(this.reconciliation);
    this.polling = null; this.reconciliation = null;
  }

  private requestIdleDelivery(): void {
    if (!this.running || this.idleRequest || !this.isIdle()) return;
    const generation = this.hostGeneration;
    const request = this.enqueueCollector("followUp", generation, undefined,
      this.discoveryRevision !== this.reconciledRevision);
    this.idleRequest = request;
    void request.catch(async (error) => {
      if (this.running && generation === this.hostGeneration) await this.recordFault("scan", error);
    }).finally(() => {
      if (this.idleRequest === request) this.idleRequest = null;
    });
  }

  async deliverAtBoundary(signal?: AbortSignal): Promise<void> {
    if (!this.running || signal?.aborted) return;
    const generation = this.hostGeneration;
    try {
      // Tool completion can create a child whose root event was missed. Always
      // rebuild candidate discovery at this awaited native boundary.
      await this.enqueueCollector("steer", generation, signal, true);
    } catch (error) {
      if (this.running && generation === this.hostGeneration) await this.recordFault("scan", error);
    }
  }

  async rows(): Promise<SpawnStateFile[]> {
    const cleared = await this.readCleared();
    return (await this.discoverChildStates()).rows.filter((row) => row.terminal === null || !cleared.has(row.id));
  }

  async clearFinished(): Promise<number> {
    const cleared = await this.readCleared();
    const terminal = (await this.discoverChildStates()).rows.filter((row) => row.terminal !== null && !cleared.has(row.id));
    for (const row of terminal) cleared.add(row.id);
    if (terminal.length) await writeJsonAtomic(this.clearedPath, {
      v: 1, spawn_id: this.currentSpawnId, updated_at_ms: Date.now(), cleared_spawn_ids: [...cleared].sort(),
    });
    this.requestIdleDelivery();
    return terminal.length;
  }

  async admitMessage(message: unknown): Promise<void> {
    if (!message || typeof message !== "object") return;
    const custom = message as { role?: unknown; customType?: unknown; details?: unknown };
    if (custom.role !== "custom" || custom.customType !== "meridian-spawn-watch") return;
    const details = custom.details as { delivery_id?: unknown; work_ids?: unknown } | undefined;
    if (typeof details?.delivery_id !== "string" || !Array.isArray(details.work_ids)) return;
    const workIds = this.queued.get(details.delivery_id);
    if (!workIds || JSON.stringify(workIds) !== JSON.stringify(details.work_ids)) return;
    // Admission of a triggerTurn message closes this native opportunity even
    // if persisting its receipt fails. Do not queue another backlog batch before
    // the admitted message's inference starts.
    this.hostGeneration++;
    this.isIdle = () => false;
    try {
      await admitDelivery(details.delivery_id, workIds, this.currentRunId, this.receiptsPath);
      await this.clearFault("admission");
    }
    catch (error) { await this.recordFault("admission", error); return; }
    this.queued.delete(details.delivery_id);
  }

  private enqueueCollector(
    deliverAs: "steer" | "followUp",
    generation: number,
    signal: AbortSignal | undefined,
    discover: boolean,
  ): Promise<void> {
    const run = this.drainTail.then(async () => {
      const completed = await this.collectAndPublish(deliverAs, generation, signal, discover);
      if (completed && this.requestIsCurrent(generation, signal)
          && this.discoveryRevision === this.reconciledRevision) await this.clearFault("scan");
    });
    this.drainTail = run.catch(() => undefined);
    return run;
  }

  private requestIsCurrent(generation: number, signal?: AbortSignal): boolean {
    return this.running && this.hostGeneration === generation && !signal?.aborted;
  }

  private canContinue(deliverAs: "steer" | "followUp", generation: number, signal?: AbortSignal): boolean {
    return this.requestIsCurrent(generation, signal) && (deliverAs === "steer" || this.isIdle());
  }

  private claimedWorkIds(): Set<string> {
    return new Set([...this.queued.values()].flat());
  }

  private async collectEligible(states: SpawnStateFile[]): Promise<NotificationItem[]> {
    const [bash, receipts, observed, cleared] = await Promise.all([
      this.readBash(), readDeliveryReceipts(this.currentRunId, this.receiptsPath),
      readSpawnObservations(this.currentSpawnId, this.observationsPath), this.readCleared(),
    ]);
    const consumed = admittedWorkIds(receipts);
    for (const id of [...observed.observed_spawn_ids, ...cleared]) consumed.add(id);
    const reserved = await reservedSpawnIds(observed);
    const claimed = this.claimedWorkIds();
    const correlated = new Set(states.map((row) => row.originating_bash_id).filter((id): id is string => !!id));
    const eligible: NotificationItem[] = [];
    for (const row of states) {
      if (row.terminal !== null && !consumed.has(row.id) && !reserved.has(row.id) && !claimed.has(row.id)) {
        eligible.push({
          id: row.id, kind: "spawn", status: row.status,
          label: `${row.agent ?? "spawn"}${row.model ? ` (${row.model})` : ""}`,
          duration: formatDurationSecs(row.terminal.duration_secs),
          exitCode: row.terminal.exit_code,
          error: row.terminal.error ?? undefined,
        });
      }
    }
    for (const record of Object.values(bash.records)) {
      const outcome = bashOutcome(record);
      if (record.is_tracked && record.is_background && isTerminalBashStatus(record.status)
          && record.notification_consumed_at_ms == null && !correlated.has(record.bash_id)
          && !consumed.has(record.bash_id) && !claimed.has(record.bash_id)) eligible.push({
        id: record.bash_id, kind: "bash", status: outcome.status, label: record.command,
        duration: formatDurationSecs(((record.ended_at_ms ?? Date.now()) - record.started_at_ms) / 1000),
        exitCode: outcome.exit_code ?? undefined, error: outcome.error,
      });
    }
    return eligible;
  }

  private async collectAndPublish(
    deliverAs: "steer" | "followUp",
    generation: number,
    signal: AbortSignal | undefined,
    discover: boolean,
  ): Promise<boolean> {
    if (!this.canContinue(deliverAs, generation, signal)) return false;
    const mayRead = () => this.canContinue(deliverAs, generation, signal);
    const discovered = discover ? await this.discoverChildStates(mayRead) : undefined;
    const initialStates = discovered?.rows ?? await this.readCandidateStates(this.candidateChildIds, mayRead);
    if (!this.canContinue(deliverAs, generation, signal)) return false;
    const initial = await this.collectEligible(initialStates);
    if (!this.canContinue(deliverAs, generation, signal)) return false;
    if (!initial.length) return true;

    // Fix membership before report I/O. The worst-case fixed frames reserve every
    // selected outcome and lookup even when an earlier optional detail is huge.
    const candidates: NotificationCandidate[] = [];
    let mandatoryBytes = 0;
    for (const item of initial) {
      if (candidates.length >= MAX_NOTIFICATION_MEMBERS) break;
      const separatorBytes = candidates.length ? Buffer.byteLength(NOTIFICATION_SEPARATOR) : 0;
      const itemMandatoryBytes = mandatoryNotificationBytes(item, this.spawnsDir);
      if (mandatoryBytes + separatorBytes + itemMandatoryBytes > MAX_NOTIFICATION_BYTES) break;
      candidates.push({ item, mandatoryBytes: itemMandatoryBytes });
      mandatoryBytes += separatorBytes + itemMandatoryBytes;
    }
    if (!candidates.length) return true;

    const selected: Array<{ item: NotificationItem; content: string }> = [];
    const separatorBytes = Buffer.byteLength(NOTIFICATION_SEPARATOR);
    let renderedBytes = 0;
    let remainingMandatoryBytes = candidates.reduce((total, candidate) => total + candidate.mandatoryBytes, 0);
    for (const [index, candidate] of candidates.entries()) {
      if (!this.canContinue(deliverAs, generation, signal)) break;
      const leadingSeparatorBytes = index ? separatorBytes : 0;
      remainingMandatoryBytes -= candidate.mandatoryBytes;
      const remainingCandidates = candidates.length - index;
      const remainingSeparatorBytes = (remainingCandidates - 1) * separatorBytes;
      const optionalBytes = MAX_NOTIFICATION_BYTES - renderedBytes - leadingSeparatorBytes
        - candidate.mandatoryBytes - remainingMandatoryBytes - remainingSeparatorBytes;
      // Split currently available detail space fairly. If this member is short,
      // renderedBytes carries the unused share forward to the remaining members.
      const maxBytes = candidate.mandatoryBytes + Math.floor(optionalBytes / remainingCandidates);
      const { item } = candidate;
      const content = item.kind === "spawn"
        ? await this.renderSpawnNotification(item, maxBytes)
        : formatBashNotification(item, maxBytes);
      if (!content) throw Error(`notification mandatory frame exceeded reservation: ${item.id}`);
      selected.push({ item, content });
      renderedBytes += leadingSeparatorBytes + Buffer.byteLength(content);
    }
    if (!this.canContinue(deliverAs, generation, signal)) return false;
    if (!selected.length) return true;

    // A cache miss cannot prove that a completed launcher has no child. Discover
    // after observing Bash terminal, so missed root events cannot split the
    // launcher's obligation from its already-published canonical child.
    const correlation = selected.some(({ item }) => item.kind === "bash")
      ? await this.discoverChildStates(mayRead) : undefined;
    const freshStates = correlation?.rows
      ?? await this.readCandidateStates(selected.map(({ item }) => item.id), mayRead);
    if (!this.canContinue(deliverAs, generation, signal)) return false;

    // Revalidate the selected IDs, not a hint cache another discovery may have
    // superseded. Observe the same idle generation around the final snapshot.
    const fresh = await this.collectEligible(freshStates);
    if (!this.canContinue(deliverAs, generation, signal)) return false;

    // This collector is deliberately finite. Work that arrived after the first
    // snapshot stays owed; initially selected work that changed is deferred.
    const freshItems = new Map(fresh.map((item) => [notificationKey(item), item]));
    // Only this scan's current committed snapshot can prove no child exists.
    // No await follows this check through claim/send; superseded Bash stays owed.
    const canPublishBash = correlation !== undefined
      && correlation.revision === this.discoveryRevision
      && correlation.revision === this.reconciledRevision;
    const batch = selected.filter(({ item }) => {
      const current = freshItems.get(notificationKey(item));
      return current !== undefined && sameNotification(item, current)
        && (item.kind !== "bash" || canPublishBash);
    });
    if (!batch.length) return true;

    const content = batch.map(({ content: memberContent }) => memberContent).join(NOTIFICATION_SEPARATOR);
    if (!this.canContinue(deliverAs, generation, signal) || !content) return false;

    const deliveryId = randomUUID();
    const workIds = batch.map(({ item }) => item.id);
    this.queued.set(deliveryId, workIds);
    try {
      this.pi.sendMessage({ customType: "meridian-spawn-watch", content, display: true,
        details: { delivery_id: deliveryId, work_ids: workIds } },
        { triggerTurn: true, deliverAs });
      // The triggering message owns this native opportunity as soon as it is
      // queued. Suppress idle wakeups until Pi reports the ensuing lifecycle.
      this.isIdle = () => false;
    } catch (error) {
      this.queued.delete(deliveryId);
      throw error;
    }
    return true;
  }

  private async renderSpawnNotification(item: NotificationItem, maxBytes: number): Promise<string | undefined> {
    const reportPath = path.join(this.spawnsDir, item.id, "report.md");
    const report = await readBoundedReport(reportPath);
    const frame = spawnNotificationFrame(item, this.spawnsDir, report.text);
    if (report.truncated) frame.truncationNotice = spawnReportTruncationNotice(reportPath);
    return renderNotificationFrame(frame, maxBytes);
  }

  private async readBash(): Promise<BashRecordsFile> {
    const value = await readPrivateJson(this.bashRecordsPath);
    if (value === undefined) return { v: 1, spawn_id: this.currentRunId, updated_at_ms: 0, records: {} };
    const file = parseBashRecordsFile(value);
    if (!file || file.spawn_id !== this.currentRunId || file.runtime_error) throw Error("invalid or unresolved bash evidence");
    return file;
  }

  private async readCleared(): Promise<Set<string>> {
    const value = await readPrivateJson(this.clearedPath);
    if (value === undefined) return new Set();
    const file = value as ClearedSpawnsFile;
    if (file.v !== 1 || file.spawn_id !== this.currentSpawnId || !Array.isArray(file.cleared_spawn_ids)
      || !Number.isFinite(file.updated_at_ms)
      || !file.cleared_spawn_ids.every((id) => typeof id === "string")) throw Error("invalid cleared spawn evidence");
    return new Set(file.cleared_spawn_ids);
  }

  private async discoverChildStates(mayRead: () => boolean = () => true): Promise<ChildDiscovery> {
    // Starting every attempt makes failure sticky even when a root event was
    // missed. Only the newest still-current attempt may publish its candidates;
    // events or overlapping discoveries leave another full scan owed.
    const revision = ++this.discoveryRevision;
    try {
      const candidates = new Set<string>();
      const rows: SpawnStateFile[] = [];
      if (existsSync(this.spawnsDir)) {
        for (const name of (await readdir(this.spawnsDir)).sort()) {
          if (!mayRead()) break;
          if (parseRef(name).spawnId === undefined) continue;
          const value = await readPrivateJson(path.join(this.spawnsDir, name, "state.json"));
          if (value === undefined) continue;
          const state = this.parseOwnedChild(name, value);
          if (state) { candidates.add(name); rows.push(state); }
        }
      }
      if (mayRead() && this.discoveryRevision === revision) {
        this.candidateChildIds = candidates;
        this.reconciledRevision = revision;
      }
      return { rows, revision };
    } catch (error) {
      // A concurrent successful scan must not make a later observed failure look
      // clean merely because that failing attempt started first.
      this.discoveryRevision++;
      throw error;
    }
  }

  private async readCandidateStates(ids: Iterable<string>, mayRead: () => boolean): Promise<SpawnStateFile[]> {
    const rows: SpawnStateFile[] = [];
    for (const name of [...ids].sort()) {
      if (!mayRead()) break;
      const value = await readPrivateJson(path.join(this.spawnsDir, name, "state.json"));
      if (value === undefined) continue;
      const state = this.parseOwnedChild(name, value);
      if (state) rows.push(state);
      else this.candidateChildIds.delete(name);
    }
    return rows;
  }

  private parseOwnedChild(name: string, value: unknown): SpawnStateFile | undefined {
    // A definitively foreign row cannot affect this parent's obligations.
    // Unknown membership and malformed own-child evidence still fail closed.
    if (value && typeof value === "object" && "id" in value && value.id === name
        && "parent_id" in value && (value.parent_id === null || typeof value.parent_id === "string")
        && value.parent_id !== this.currentSpawnId) return undefined;
    const state = parseSpawnStateFile(value);
    if (!state || state.id !== name) throw Error(`invalid spawn state: ${name}`);
    return state.parent_id === this.currentSpawnId ? state : undefined;
  }

  private logWarning(error: unknown): void {
    process.stderr.write(`[meridian-spawn-watch] ${String(error)}\n`);
  }

  private async recordFault(operation: "scan" | "admission", error: unknown): Promise<void> {
    this.logWarning(error);
    try { await writeJsonAtomic(this.deliveryFaultPath, {
      // Persisted for compatibility; spawn_id contains this run-scoped file's exact run ID.
      v: 1, spawn_id: this.currentRunId, operation, error: String(error),
    }); } catch (writeError) { this.logWarning(writeError); }
  }

  private async clearFault(operation: "scan" | "admission"): Promise<void> {
    const value = await readPrivateJson(this.deliveryFaultPath) as { operation?: unknown; error?: unknown } | undefined;
    if (!value || !value.error || value.operation !== operation) return;
    await writeJsonAtomic(this.deliveryFaultPath, { v: 1, spawn_id: this.currentRunId, operation: null, error: null });
  }
}

async function readBoundedReport(reportPath: string): Promise<{ text: string | null; truncated: boolean }> {
  let report;
  try { report = await open(reportPath, "r"); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return { text: null, truncated: false };
    throw error;
  }
  try {
    const buffer = Buffer.alloc(MAX_REPORT_BYTES + 1);
    let bytesRead = 0;
    while (bytesRead < buffer.length) {
      const chunk = await report.read(buffer, bytesRead, buffer.length - bytesRead, null);
      if (chunk.bytesRead === 0) break;
      bytesRead += chunk.bytesRead;
    }
    return {
      text: buffer.subarray(0, Math.min(bytesRead, MAX_REPORT_BYTES)).toString("utf8").trim() || null,
      truncated: bytesRead > MAX_REPORT_BYTES,
    };
  } finally {
    await report.close();
  }
}

function notificationKey(item: NotificationItem): string {
  return `${item.kind}:${item.id}`;
}

function sameNotification(left: NotificationItem, right: NotificationItem): boolean {
  return left.kind === right.kind && left.id === right.id && left.status === right.status
    && left.label === right.label && left.duration === right.duration
    && left.exitCode === right.exitCode && left.error === right.error;
}

function notificationOutcome(item: NotificationItem): string {
  const code = item.exitCode === undefined ? "" : `exit ${item.exitCode}, `;
  // A diagnostic can be optional; the fact that the result has an error cannot.
  return `${code}${item.duration}${item.error === undefined ? "" : "; result error"}`;
}

function spawnNotificationFrame(item: NotificationItem, spawnsDir: string, report: string | null): NotificationFrame {
  const reportPath = path.join(spawnsDir, item.id, "report.md");
  const details = [`Worker: ${item.label}`];
  if (item.error) details.push(`Failure: ${item.error}`);
  details.push("", report || "No durable report content was published.");
  return {
    prefix: `${item.id} ${item.status} (${notificationOutcome(item)})`,
    detail: details.join("\n"),
    truncationNotice: `[Optional spawn details truncated to fit ${MAX_NOTIFICATION_BYTES} UTF-8 bytes.]`,
    lookup: `Full report: ${reportPath}\nTranscript: meridian session log ${item.id}`,
  };
}

function spawnReportTruncationNotice(reportPath: string): string {
  return [
    `[Report truncated after ${MAX_REPORT_BYTES} bytes; full report: ${reportPath}]`,
    `[Optional spawn details truncated to fit ${MAX_NOTIFICATION_BYTES} UTF-8 bytes.]`,
  ].join("\n");
}

function bashNotificationFrame(item: NotificationItem): NotificationFrame {
  return {
    prefix: `Background bash ${item.id} ${item.status} (${notificationOutcome(item)}).`,
    detail: [item.error ? `Failure: ${item.error}` : "", `Command: ${item.label}`].filter(Boolean).join("\n"),
    truncationNotice: `[Optional Bash details truncated to fit ${MAX_NOTIFICATION_BYTES} UTF-8 bytes.]`,
    lookup: `Full output: bash_manage({action: "output", bash_id: "${item.id}"})`,
  };
}

function formatBashNotification(item: NotificationItem, maxBytes: number): string | undefined {
  return renderNotificationFrame(bashNotificationFrame(item), maxBytes);
}

function mandatoryNotificationBytes(item: NotificationItem, spawnsDir: string): number {
  const frame = item.kind === "spawn"
    ? spawnNotificationFrame(item, spawnsDir, null)
    : bashNotificationFrame(item);
  if (item.kind === "spawn") {
    const reportPath = path.join(spawnsDir, item.id, "report.md");
    frame.truncationNotice = spawnReportTruncationNotice(reportPath);
  }
  return Buffer.byteLength(`${frame.prefix}\n\n${frame.truncationNotice}\n${frame.lookup}`);
}

function renderNotificationFrame(frame: NotificationFrame, maxBytes: number): string | undefined {
  const full = `${frame.prefix}\n${frame.detail}\n${frame.lookup}`;
  if (Buffer.byteLength(full) <= maxBytes) return full;
  const detailPrefix = `${frame.prefix}\n`;
  const suffix = `\n${frame.truncationNotice}\n${frame.lookup}`;
  const fixedBytes = Buffer.byteLength(detailPrefix) + Buffer.byteLength(suffix);
  if (fixedBytes > maxBytes) return undefined;
  const detail = truncateUtf8(frame.detail, maxBytes - fixedBytes).trimEnd();
  return `${detailPrefix}${detail}${suffix}`;
}

function truncateUtf8(value: string, maxBytes: number): string {
  const encoded = Buffer.from(value);
  if (encoded.length <= maxBytes) return value;
  let end = maxBytes;
  while (end > 0 && (encoded[end]! & 0xc0) === 0x80) end--;
  return encoded.subarray(0, end).toString("utf8");
}

function formatSpawnStatus(row: SpawnStateFile, theme: Theme): string {
  const status = String(row.status ?? "unknown").toLowerCase();
  const dim = (value: string) => theme.fg("dim", value);
  const success = (value: string) => theme.fg("success", value);
  const error = (value: string) => theme.fg("error", value);
  const warning = (value: string) => theme.fg("warning", value);

  if (row.terminal === null) return success(`● ${status}`);
  if (status === "succeeded") return dim("✓ succeeded");
  if (status === "failed") return error("✗ failed");
  if (status === "cancelled" || status === "canceled") return warning(`✗ ${status}`);
  if (status === "timed_out") return error("✗ timed_out");
  return dim(`✓ ${status}`);
}

function renderSpawnPreview(row: SpawnStateFile, theme: Theme): string[] {
  const dim = (value: string) => theme.fg("dim", value);
  const lines = [
    `${theme.fg("accent", row.id)} ${formatSpawnStatus(row, theme)} ${dim(formatDurationSecs(row.terminal?.duration_secs))}`,
    dim(`${row.agent ?? "spawn"}${row.model ? ` · ${row.model}` : ""}`),
  ];
  if (row.originating_bash_id) lines.push(dim(`launched by ${row.originating_bash_id}`));
  if (row.started_at) lines.push(dim(`started ${row.started_at}`));
  if (row.terminal?.finished_at) lines.push(dim(`finished ${row.terminal.finished_at}`));
  return lines;
}

const SPAWN_PANEL_COLUMNS: SelectablePanelColumn<SpawnStateFile>[] = [
  { header: "ID", width: 10, render: (row, theme, selected) => (theme ? (selected ? theme.fg("accent", row.id) : theme.fg("dim", row.id)) : row.id) },
  { header: "STATUS", width: 14, render: (row, theme) => (theme ? formatSpawnStatus(row, theme) : String(row.status ?? "")) },
  { header: "DUR", width: 8, render: (row, theme) => (theme ? theme.fg("dim", formatDurationSecs(row.terminal?.duration_secs)) : formatDurationSecs(row.terminal?.duration_secs)), align: "right" },
  { header: "AGENT", width: 16, render: (row) => String(row.agent ?? "") },
  { header: "MODEL", width: 24, render: (row, theme) => (theme ? theme.fg("dim", String(row.model ?? "")) : String(row.model ?? "")) },
  { header: "← BASH", width: 10, render: (row, theme) => (theme ? theme.fg("dim", String(row.originating_bash_id ?? "")) : String(row.originating_bash_id ?? "")) },
];

export default function meridianSpawnWatchExtension(pi: ExtensionAPI): void {
  const key = Symbol.for("meridian.spawn-watch.runtimes");
  const registry = globalThis as typeof globalThis & { [key]: Map<string, SpawnWatchRuntime> | undefined };
  const owners = registry[key] ??= new Map();
  const ownerPath = resolveRunScopeDir(currentRunIdFromEnv());
  const runtime = owners.get(ownerPath) ?? new SpawnWatchRuntime(pi);
  owners.set(ownerPath, runtime);
  runtime.rebind(pi);
  pi.on?.("session_start", (_event, ctx) => { runtime.observeIdle(() => ctx.isIdle()); runtime.start(); });
  pi.on?.("agent_start", (_event, ctx) => runtime.observeActive(() => ctx.isIdle()));
  pi.on?.("agent_end", (_event, ctx) => runtime.observeIdle(() => ctx.isIdle()));
  pi.on?.("turn_end", async (event, ctx) => {
    if (event.outcome !== "completed" || event.toolResults.length === 0 || ctx.signal?.aborted) return;
    await runtime.deliverAtBoundary(ctx.signal);
  });
  pi.on?.("message_start", async (event) => runtime.admitMessage(event.message));
  pi.on?.("session_shutdown", (event) => {
    runtime.stop();
    if (event.reason !== "reload") owners.delete(ownerPath);
  });

  pi.registerCommand("spawn", {
    description: "List Meridian spawns correlated to this Pi session.",
    handler: async (_args, ctx) => {
      const loadRows = async (): Promise<SpawnStateFile[]> => runtime.rows();

      if (!hasTaskPanelUI(ctx)) {
        const rows = await loadRows();
        const text = rows.length ? renderTable(SPAWN_PANEL_COLUMNS, rows, 100).join("\n") : "No correlated Meridian spawns.";
        showTaskText(ctx, text);
        return;
      }

      await openTaskPanel(ctx as PanelCommandContext, {
        title: "Meridian /spawn — correlated spawns",
        columns: SPAWN_PANEL_COLUMNS,
        loadRows,
        getRowId: (row) => row.id,
        renderPreview: renderSpawnPreview,
        emptyMessage: "No correlated Meridian spawns.",
        footer: "enter logs · c clear · j/k select · r refresh · q close",
        onClear: async () => {
          const cleared = await runtime.clearFinished();
          ctx.ui?.notify?.(`cleared ${cleared} finished spawn(s)`, "info");
        },
        onEnter: async (row) => {
          await openLogOverlay(ctx as PanelCommandContext, {
            title: `Spawn log ${row.id}`,
            initialFollow: row.terminal === null,
            refreshIntervalMs: 2000,
            streams: [
              {
                id: "log",
                label: "log",
                loadText: async () => {
                  const result = await runMeridianCommand(["session", "log", row.id], 15_000);
                  const text = (result.stdout || result.stderr).trimEnd();
                  if (text) return text;
                  const showResult = await runMeridianCommand(["spawn", "show", row.id], 15_000);
                  return (showResult.stdout || showResult.stderr).trimEnd() || `No output for ${row.id}`;
                },
              },
            ],
          });
        },
      });
    },
  });

  pi.registerCommand("spawn:clear", {
    description: "Clear finished correlated Meridian spawns from /spawn.",
    handler: async (_args, ctx) => {
      const cleared = await runtime.clearFinished();
      ctx.ui.notify(`cleared ${cleared} finished spawn(s)`, "info");
    },
  });

  pi.registerCommand("spawn:show", {
    description: "Show a correlated Meridian spawn.",
    handler: async (args, ctx) => {
      const result = await runMeridianCommand(["spawn", "show", args.trim()], 15_000);
      ctx.ui.notify(result.stdout || result.stderr, result.exitCode === 0 ? "info" : "error");
    },
  });

  pi.registerCommand("spawn:wait", {
    description: "Wait for a correlated Meridian spawn.",
    handler: async (args, ctx) => {
      const result = await runMeridianCommand(["spawn", "wait", args.trim()], 60_000);
      ctx.ui.notify(result.stdout || result.stderr, result.exitCode === 0 ? "info" : "error");
    },
  });

  pi.registerCommand("spawn:cancel", {
    description: "Cancel a correlated Meridian spawn.",
    handler: async (args, ctx) => {
      const result = await runMeridianCommand(["spawn", "cancel", args.trim()], 15_000);
      ctx.ui.notify(result.stdout || result.stderr, result.exitCode === 0 ? "info" : "error");
    },
  });

  pi.registerCommand("spawn:log", {
    description: "Show recent log for a correlated Meridian spawn.",
    handler: async (args, ctx) => {
      const result = await runMeridianCommand(["session", "log", args.trim()], 15_000);
      ctx.ui.notify(result.stdout || result.stderr, result.exitCode === 0 ? "info" : "error");
    },
  });
}
