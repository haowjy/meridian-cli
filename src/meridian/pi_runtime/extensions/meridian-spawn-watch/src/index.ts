import { existsSync, mkdirSync, readdirSync, watch, type FSWatcher } from "node:fs";
import { randomUUID } from "node:crypto";
import path from "node:path";

import type { ExtensionAPI, Theme } from "@earendil-works/pi-coding-agent";

import { writeJsonAtomic } from "../../shared/json_file";
import { runMeridianCommand } from "../../shared/meridian_cli";
import { admitDelivery, admittedWorkIds, readDeliveryReceipts, readPrivateJson, readSpawnObservations, reservedSpawnIds } from "../../shared/delivery_receipts";
import {
  currentSpawnIdFromEnv,
  resolveBashRecordsPath,
  resolveClearedSpawnsPath,
  resolvePiBashDir,
  resolveSpawnsDir,
} from "../../shared/pi_state_paths";
import type { BashRecordsFile, SpawnStateFile } from "../../shared/schemas";
import { isTerminalBashStatus, parseBashRecordsFile, parseSpawnStateFile } from "../../shared/schemas";
import { openLogOverlay } from "../../shared/log_overlay";
import {
  openTaskPanel,
  type PanelCommandContext,
  type SelectablePanelColumn,
} from "../../shared/selectable_panel";
import { formatDurationSecs, renderTable } from "../../shared/ui";

type NotificationItem = {
  id: string;
  kind: "spawn" | "bash";
  status: string;
  label: string;
  duration: string;
};
type ClearedSpawnsFile = { v: 1; spawn_id: string; updated_at_ms: number; cleared_spawn_ids: string[] };
const SCAN_DELAY_MS = 200;
const POLL_MS = 500;

/** Launch-scoped owner: durable rows are obligations; queue claims are only mechanics. */
export class SpawnWatchRuntime {
  private readonly currentSpawnId = currentSpawnIdFromEnv();
  private readonly spawnsDir = resolveSpawnsDir();
  private readonly bashDir = resolvePiBashDir(this.currentSpawnId);
  private readonly bashRecordsPath = resolveBashRecordsPath(this.currentSpawnId);
  private readonly clearedPath = resolveClearedSpawnsPath(this.currentSpawnId);
  private readonly receiptsPath = path.join(this.bashDir, "delivery-receipts.json");
  private readonly observationsPath = path.join(this.bashDir, "observed-spawns.json");
  private readonly pending = new Map<string, NotificationItem>();
  private readonly queued = new Map<string, string[]>();
  private readonly inFlight = new Set<string>();
  private watchers: FSWatcher[] = [];
  private scanScheduled: NodeJS.Timeout | null = null;
  private polling: NodeJS.Timeout | null = null;
  private running = false;
  private scanRunning = false;
  private scanAgain = false;
  private flushing = false;

  constructor(private pi: ExtensionAPI, private isIdle: () => boolean = () => false) {}
  rebind(pi: ExtensionAPI): void { this.pi = pi; }
  observeIdle(check: () => boolean): void { this.isIdle = check; this.requestScan(); }

  start(): void {
    if (this.running) return;
    this.running = true;
    mkdirSync(this.spawnsDir, { recursive: true });
    mkdirSync(this.bashDir, { recursive: true });
    for (const dir of [this.spawnsDir, this.bashDir]) {
      try {
        const watcher = watch(dir, { recursive: false }, () => this.requestScan());
        watcher.on("error", (error) => { this.logWarning(error); watcher.close(); });
        watcher.unref(); this.watchers.push(watcher);
      } catch (error) { this.logWarning(error); }
    }
    // Child state writes do not notify the nonrecursive parent watch. Polling
    // guarantees progress; events merely accelerate this same snapshot read.
    this.polling = setInterval(() => this.requestScan(), POLL_MS);
    this.polling.unref();
    this.requestScan();
  }

  stop(): void {
    this.running = false;
    for (const watcher of this.watchers) watcher.close();
    this.watchers = [];
    if (this.scanScheduled) clearTimeout(this.scanScheduled);
    if (this.polling) clearInterval(this.polling);
    this.scanScheduled = null; this.polling = null;
  }

  private requestScan(): void {
    if (!this.running || this.scanScheduled) return;
    this.scanScheduled = setTimeout(() => {
      this.scanScheduled = null;
      void this.scan().catch((error) => this.recordFault("scan", error));
    }, SCAN_DELAY_MS);
    this.scanScheduled.unref();
  }

  async rows(_discover = false): Promise<SpawnStateFile[]> {
    const cleared = await this.readCleared();
    return (await this.readChildStates()).filter((row) => row.terminal === null || !cleared.has(row.id));
  }

  async clearFinished(): Promise<number> {
    const cleared = await this.readCleared();
    const terminal = (await this.readChildStates()).filter((row) => row.terminal !== null && !cleared.has(row.id));
    for (const row of terminal) cleared.add(row.id);
    if (terminal.length) await writeJsonAtomic(this.clearedPath, {
      v: 1, spawn_id: this.currentSpawnId, updated_at_ms: Date.now(), cleared_spawn_ids: [...cleared].sort(),
    });
    this.requestScan();
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
    try { await admitDelivery(details.delivery_id, workIds, this.currentSpawnId, this.receiptsPath); await this.clearFault(); }
    catch (error) { await this.recordFault("admission", error); return; }
    this.queued.delete(details.delivery_id);
    for (const id of workIds) { this.inFlight.delete(id); this.pending.delete(id); }
    this.requestScan();
  }

  private async scan(): Promise<void> {
    if (!this.running) return;
    if (this.scanRunning) { this.scanAgain = true; return; }
    this.scanRunning = true;
    try {
      do {
        this.scanAgain = false;
        const [states, bash, receipts, observed, cleared] = await Promise.all([
          this.readChildStates(), this.readBash(), readDeliveryReceipts(this.currentSpawnId, this.receiptsPath),
          readSpawnObservations(this.currentSpawnId, this.observationsPath), this.readCleared(),
        ]);
        const consumed = admittedWorkIds(receipts);
        for (const id of [...observed.observed_spawn_ids, ...cleared]) consumed.add(id);
        const reserved = await reservedSpawnIds(observed);
        const correlated = new Set(states.map((row) => row.originating_bash_id).filter((id): id is string => !!id));
        const eligible: NotificationItem[] = [];
        for (const row of states) {
          if (row.terminal !== null && !consumed.has(row.id) && !reserved.has(row.id)) eligible.push({
            id: row.id, kind: "spawn", status: row.status,
            label: `${row.agent ?? "spawn"}${row.model ? ` (${row.model})` : ""}`,
            duration: formatDurationSecs(row.terminal.duration_secs),
          });
        }
        for (const record of Object.values(bash.records)) {
          if (record.is_tracked && record.is_background && isTerminalBashStatus(record.status)
              && record.notification_consumed_at_ms == null && !correlated.has(record.bash_id)
              && !consumed.has(record.bash_id)) eligible.push({
            id: record.bash_id, kind: "bash", status: record.status, label: record.command,
            duration: formatDurationSecs(((record.ended_at_ms ?? Date.now()) - record.started_at_ms) / 1000),
          });
        }
        const ids = new Set(eligible.map((item) => item.id));
        for (const id of this.pending.keys()) if (!ids.has(id)) this.pending.delete(id);
        for (const item of eligible) if (!this.inFlight.has(item.id)) this.pending.set(item.id, item);
        await this.flush();
        await this.clearFault("scan");
      } while (this.running && this.scanAgain);
    } finally { this.scanRunning = false; }
  }

  private async flush(): Promise<void> {
    if (!this.running || this.flushing || !this.pending.size) return;
    this.flushing = true;
    try {
      const batch = [...this.pending.values()];
      const spawnItems = batch.filter((item) => item.kind === "spawn");
      const spawnContent = spawnItems.length ? await formatSpawnWaitNotification(spawnItems.map((item) => item.id), spawnItems) : "";
      // Formatter crosses a CLI await. Reservations/explicit consumption can
      // have changed while it ran, so eligibility is read again before queueing.
      const [receipts, observed, bash, cleared] = await Promise.all([
        readDeliveryReceipts(this.currentSpawnId, this.receiptsPath), readSpawnObservations(this.currentSpawnId, this.observationsPath), this.readBash(), this.readCleared(),
      ]);
      const consumed = admittedWorkIds(receipts);
      for (const id of [...observed.observed_spawn_ids, ...cleared]) consumed.add(id);
      const reserved = await reservedSpawnIds(observed);
      const eligible = batch.filter((item) => !consumed.has(item.id) && !this.inFlight.has(item.id)
        && (item.kind === "spawn" ? !reserved.has(item.id) : bash.records[item.id]?.notification_consumed_at_ms == null));
      // If the child selection changed, formatting must follow that selection.
      // Retry the fresh snapshot instead of sending stale consumed child output.
      const selectedSpawns = eligible.filter((item) => item.kind === "spawn");
      if (JSON.stringify(selectedSpawns.map((item) => item.id)) !== JSON.stringify(spawnItems.map((item) => item.id))) {
        for (const item of batch) if (!eligible.some((row) => row.id === item.id)) this.pending.delete(item.id);
        this.scanAgain = true; return;
      }
      const bashItems = eligible.filter((item) => item.kind === "bash");
      const content = [spawnContent, bashItems.length ? formatBashNotification(bashItems) : ""].filter(Boolean).join("\n\n");
      if (!this.running || !this.isIdle() || !content || !eligible.length) return;
      const deliveryId = randomUUID();
      const workIds = eligible.map((item) => item.id);
      this.queued.set(deliveryId, workIds);
      for (const id of workIds) this.inFlight.add(id);
      try {
        this.pi.sendMessage({ customType: "meridian-spawn-watch", content, display: true,
          details: { delivery_id: deliveryId, work_ids: workIds, ids: workIds } },
          { triggerTurn: true, deliverAs: "followUp" });
      } catch (error) {
        this.queued.delete(deliveryId);
        for (const id of workIds) this.inFlight.delete(id);
        throw error;
      }
      for (const id of workIds) this.pending.delete(id);
    } finally { this.flushing = false; }
  }

  private async readBash(): Promise<BashRecordsFile> {
    const value = await readPrivateJson(this.bashRecordsPath);
    if (value === undefined) return { v: 1, spawn_id: this.currentSpawnId, updated_at_ms: 0, records: {} };
    const file = parseBashRecordsFile(value);
    if (!file || file.spawn_id !== this.currentSpawnId || file.runtime_error) throw Error("invalid or unresolved bash evidence");
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

  private async readChildStates(): Promise<SpawnStateFile[]> {
    if (!existsSync(this.spawnsDir)) return [];
    const rows: SpawnStateFile[] = [];
    for (const name of readdirSync(this.spawnsDir)) {
      if (!name.startsWith("p")) continue;
      const value = await readPrivateJson(path.join(this.spawnsDir, name, "state.json"));
      if (value === undefined) continue;
      const state = parseSpawnStateFile(value);
      if (!state) throw Error(`invalid spawn state: ${name}`);
      if (state.id === name && state.parent_id === this.currentSpawnId) rows.push(state);
    }
    return rows;
  }

  private logWarning(error: unknown): void {
    process.stderr.write(`[meridian-spawn-watch] ${String(error)}\n`);
  }

  private async recordFault(operation: "scan" | "admission", error: unknown): Promise<void> {
    this.logWarning(error);
    try { await writeJsonAtomic(path.join(this.bashDir, "delivery-fault.json"), {
      v: 1, spawn_id: this.currentSpawnId, operation, error: String(error),
    }); } catch (writeError) { this.logWarning(writeError); }
  }

  private async clearFault(operation?: "scan"): Promise<void> {
    const file = path.join(this.bashDir, "delivery-fault.json");
    const value = await readPrivateJson(file) as { operation?: unknown; error?: unknown } | undefined;
    if (!value || !value.error || (operation && value.operation !== operation && this.inFlight.size)) return;
    await writeJsonAtomic(file, { v: 1, spawn_id: this.currentSpawnId, operation: null, error: null });
  }
}

async function formatSpawnWaitNotification(
  spawnIds: string[],
  fallbackItems: NotificationItem[],
): Promise<string> {
  const result = await runMeridianCommand(["spawn", "wait", ...spawnIds, "--no-observe"], 30_000);
  const output = (result.stdout || result.stderr).trimEnd();
  if (result.exitCode === 0 && output.length > 0) return output;

  const fallbackSpawnItems = fallbackItems.filter((item) => item.kind === "spawn");
  if (fallbackSpawnItems.length === 1) {
    const item = fallbackSpawnItems[0]!;
    return `Spawn ${item.id} completed (${item.label}, ${item.duration}): ${item.status}\nUse \`meridian spawn wait ${item.id}\` for details.`;
  }
  return [
    "Meridian spawns completed:",
    ...fallbackSpawnItems.map((item) => `- ${item.id} (${item.label}, ${item.duration}) ${item.status}`),
    `Use \`meridian spawn wait ${spawnIds.join(" ")}\` for details.`,
  ].join("\n");
}

function formatBashNotification(items: NotificationItem[]): string {
  if (items.length === 1) {
    const item = items[0]!;
    return `Background bash ${item.id} completed (${item.label}, ${item.duration}): ${item.status}\nUse \`bash_manage({action: "output", bash_id: "${item.id}"})\` for details.`;
  }
  return [
    "Background bash tasks completed:",
    ...items.map((item) => `- ${item.id} (${item.label}, ${item.duration}) ${item.status}`),
    "Use `bash_manage(action='output')` for details.",
  ].join("\n");
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
  const ownerPath = resolvePiBashDir();
  const runtime = owners.get(ownerPath) ?? new SpawnWatchRuntime(pi);
  owners.set(ownerPath, runtime);
  runtime.rebind(pi);
  pi.on?.("session_start", (_event, ctx) => { runtime.observeIdle(() => ctx.isIdle()); runtime.start(); });
  pi.on?.("agent_start", (_event, ctx) => runtime.observeIdle(() => ctx.isIdle()));
  pi.on?.("agent_end", (_event, ctx) => runtime.observeIdle(() => ctx.isIdle()));
  pi.on?.("message_start", async (event) => runtime.admitMessage(event.message));
  pi.on?.("session_shutdown", (event) => {
    if (event.reason === "reload") return;
    runtime.stop(); owners.delete(ownerPath);
  });

  pi.registerCommand("spawn", {
    description: "List Meridian spawns correlated to this Pi session.",
    handler: async (_args, ctx) => {
      const loadRows = async (): Promise<SpawnStateFile[]> => runtime.rows(true);

      if (ctx.hasUI === false || !ctx.ui?.custom) {
        const rows = await loadRows();
        const text = rows.length ? renderTable(SPAWN_PANEL_COLUMNS, rows, 100).join("\n") : "No correlated Meridian spawns.";
        ctx.ui.notify(text, "info");
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
