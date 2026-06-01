import { existsSync, mkdirSync, readdirSync, watch, type FSWatcher } from "node:fs";
import path from "node:path";

import type { ExtensionAPI, Theme } from "@earendil-works/pi-coding-agent";

import { readJsonFile, writeJsonAtomic } from "../../shared/json_file";
import { runMeridianCommand } from "../../shared/meridian_cli";
import { isSpawnLauncherCommand } from "../../shared/meridian_spawn";
import { readSpawnOriginBashIds } from "../../shared/spawn_origins";
import {
  currentSpawnIdFromEnv,
  resolveBashRecordsPath,
  resolveClearedSpawnsPath,
  resolveLastNotificationPath,
  resolveObservedSpawnsPath,
  resolvePiBashDir,
  resolveSpawnsDir,
} from "../../shared/pi_state_paths";
import type { BashRecordsFile, ObservedSpawnsFile, SpawnStateFile } from "../../shared/schemas";
import { isTerminalBashStatus, isTerminalSpawnStatus } from "../../shared/schemas";
import { openLogOverlay } from "../../shared/log_overlay";
import {
  openTaskPanel,
  type PanelCommandContext,
  type SelectablePanelColumn,
} from "../../shared/selectable_panel";
import { formatDurationSecs, renderTable } from "../../shared/ui";

type PiWithMessages = ExtensionAPI & {
  sendMessage?: (
    message: { customType?: string; content: string; display?: boolean; details?: Record<string, unknown> },
    options?: { triggerTurn?: boolean; deliverAs?: "steer" | "followUp" | "nextTurn" },
  ) => void | Promise<void>;
};

type ClearedSpawnsFile = {
  v: 1;
  spawn_id: string;
  updated_at_ms: number;
  cleared_spawn_ids: string[];
};

type NotificationItem = {
  id: string;
  kind: "spawn" | "bash";
  status: string;
  label: string;
  duration: string;
};

const TERMINAL_NOTIFIED = new Set<string>();
const DEBOUNCE_MS = 200;
const MAX_WAVE_WAIT_MS = 2_000;
const BASH_SPAWN_CORRELATION_GRACE_MS = 2_500;
const FALLBACK_SCAN_MS = 5_000;
const SPAWN_DISCOVERY_SCAN_MS = 500;
const SPAWN_DISCOVERY_POLL_MS = 15_000;

export class SpawnWatchRuntime {
  private readonly currentSpawnId = currentSpawnIdFromEnv();
  private readonly spawnsDir = resolveSpawnsDir();
  private readonly bashDir = resolvePiBashDir(this.currentSpawnId);
  private readonly bashRecordsPath = resolveBashRecordsPath(this.currentSpawnId);
  private readonly clearedPath = resolveClearedSpawnsPath(this.currentSpawnId);
  private readonly observedPath = resolveObservedSpawnsPath(this.currentSpawnId);
  private readonly markerPath = resolveLastNotificationPath(this.currentSpawnId);
  private readonly pending = new Map<string, NotificationItem>();
  private readonly clearedSpawnIds = new Set<string>();
  private readonly originBashIds = new Set<string>();
  private readonly missingStateSpawnIds = new Set<string>();
  private clearedLoaded = false;
  private originsLoaded = false;
  private watchers: Array<FSWatcher | null> = [];
  private debounce: NodeJS.Timeout | null = null;
  private maxWave: NodeJS.Timeout | null = null;
  private scanScheduled: NodeJS.Timeout | null = null;
  private fallbackScanInterval: NodeJS.Timeout | null = null;
  private discoveryScanInterval: NodeJS.Timeout | null = null;
  private discoveryPollTimer: NodeJS.Timeout | null = null;
  private readonly fallbackScanReasons = new Set<string>();
  private readonly bashGraceTimers = new Map<string, NodeJS.Timeout>();
  private running = false;
  private scanRunning = false;
  private scanAgain = false;

  constructor(private readonly pi: PiWithMessages) {}

  start(): void {
    this.running = true;
    mkdirSync(this.spawnsDir, { recursive: true });
    mkdirSync(this.bashDir, { recursive: true });

    const topLevelWatcher = this.watchTopLevelSpawnsDir();
    this.addWatcher(topLevelWatcher);
    if (!topLevelWatcher) this.enableFallbackScan("spawns-dir");

    const bashWatcher = this.watchDirectory(this.bashDir, () => this.requestScan(), {
      onError: () => this.enableFallbackScan("bash-dir"),
    });
    this.addWatcher(bashWatcher);
    if (!bashWatcher) this.enableFallbackScan("bash-dir");
    this.enableDiscoveryPollingForMissingSpawnStates();
    void this.scan();
  }

  stop(): void {
    this.running = false;
    for (const watcher of this.watchers) {
      if (watcher) this.closeWatcher(watcher, "session watcher");
    }
    this.watchers = [];
    if (this.debounce) clearTimeout(this.debounce);
    if (this.maxWave) clearTimeout(this.maxWave);
    if (this.scanScheduled) clearTimeout(this.scanScheduled);
    if (this.fallbackScanInterval) clearInterval(this.fallbackScanInterval);
    if (this.discoveryScanInterval) clearInterval(this.discoveryScanInterval);
    if (this.discoveryPollTimer) clearTimeout(this.discoveryPollTimer);
    this.fallbackScanInterval = null;
    this.discoveryScanInterval = null;
    this.discoveryPollTimer = null;
    this.fallbackScanReasons.clear();
    for (const timer of this.bashGraceTimers.values()) clearTimeout(timer);
    this.bashGraceTimers.clear();
  }

  async rows(discover = false): Promise<SpawnStateFile[]> {
    await this.loadClearedSpawnIds();
    if (discover) this.discoverMissingSpawnStates({ force: true });
    return (await this.readOriginSpawnStates()).filter((state) => !this.isClearedTerminalSpawn(state));
  }

  async clearFinished(): Promise<number> {
    await this.loadClearedSpawnIds();
    const terminalStates = (await this.readOriginSpawnStates()).filter(
      (state) =>
        isTerminalSpawnStatus(String(state.status ?? "")) &&
        !this.clearedSpawnIds.has(state.id),
    );
    for (const state of terminalStates) {
      this.clearedSpawnIds.add(state.id);
      this.pending.delete(state.id);
      TERMINAL_NOTIFIED.add(state.id);
    }
    if (terminalStates.length > 0) await this.persistClearedSpawnIds();
    return terminalStates.length;
  }

  private watchTopLevelSpawnsDir(): FSWatcher | null {
    let watcher: FSWatcher | null = null;
    watcher = this.watchDirectory(
      this.spawnsDir,
      (_eventType, filename) => {
        const name = filename ? path.basename(filename.toString()) : "";
        if (name.startsWith("p")) this.enableDiscoveryPolling();
        this.requestScan();
      },
      {
        onError: () => {
          if (watcher) this.removeWatcher(watcher);
          this.enableFallbackScan("spawns-dir");
        },
      },
    );
    return watcher;
  }

  private watchDirectory(
    dir: string,
    onChange: (eventType: string, filename: string | Buffer | null) => void,
    options: { onError?: (error: unknown) => void } = {},
  ): FSWatcher | null {
    try {
      mkdirSync(dir, { recursive: true });
      const watcher = watch(dir, { recursive: false }, onChange);
      watcher.on("error", (error) => {
        this.logWarning(`watcher error for ${dir}: ${this.formatError(error)}`);
        this.closeWatcher(watcher, `watcher for ${dir}`);
        options.onError?.(error);
      });
      watcher.unref();
      return watcher;
    } catch (error) {
      this.logWarning(`failed to watch ${dir}: ${this.formatError(error)}; falling back to polling where available`);
      return null;
    }
  }

  private addWatcher(watcher: FSWatcher | null): void {
    if (watcher) this.watchers.push(watcher);
  }

  private removeWatcher(watcher: FSWatcher): void {
    this.watchers = this.watchers.filter((candidate) => candidate !== watcher);
  }

  private enableFallbackScan(reason: string): void {
    if (!this.running) return;
    this.fallbackScanReasons.add(reason);
    if (this.fallbackScanInterval) return;
    this.fallbackScanInterval = setInterval(() => this.requestScan(), FALLBACK_SCAN_MS);
    this.fallbackScanInterval.unref();
    this.requestScan();
  }

  private disableFallbackScan(reason: string): void {
    this.fallbackScanReasons.delete(reason);
    if (this.fallbackScanReasons.size > 0 || !this.fallbackScanInterval) return;
    clearInterval(this.fallbackScanInterval);
    this.fallbackScanInterval = null;
  }

  private enableDiscoveryPolling(): void {
    if (!this.running) return;
    this.fallbackScanReasons.add("spawn-discovery");
    if (!this.discoveryScanInterval) {
      this.discoveryScanInterval = setInterval(() => this.requestScan(), SPAWN_DISCOVERY_SCAN_MS);
      this.discoveryScanInterval.unref();
    }
    if (this.discoveryPollTimer) clearTimeout(this.discoveryPollTimer);
    this.discoveryPollTimer = setTimeout(() => {
      this.stopDiscoveryPolling();
    }, SPAWN_DISCOVERY_POLL_MS);
    this.discoveryPollTimer.unref();
    this.requestScan();
  }

  private stopDiscoveryPolling(): void {
    if (this.discoveryScanInterval) clearInterval(this.discoveryScanInterval);
    if (this.discoveryPollTimer) clearTimeout(this.discoveryPollTimer);
    this.discoveryScanInterval = null;
    this.discoveryPollTimer = null;
    this.disableFallbackScan("spawn-discovery");
    this.requestScan();
  }

  private enableDiscoveryPollingForMissingSpawnStates(): void {
    this.discoverMissingSpawnStates();
  }

  private discoverMissingSpawnStates(options: { force?: boolean } = {}): void {
    if (!existsSync(this.spawnsDir)) return;
    let foundNewMissingState = false;
    for (const name of readdirSync(this.spawnsDir)) {
      if (
        name.startsWith("p") &&
        !existsSync(this.spawnStatePath(name)) &&
        (options.force === true || !this.missingStateSpawnIds.has(name))
      ) {
        this.missingStateSpawnIds.add(name);
        foundNewMissingState = true;
      }
    }
    if (foundNewMissingState) this.enableDiscoveryPolling();
  }

  private closeWatcher(watcher: FSWatcher, context: string): void {
    try {
      watcher.close();
    } catch (error) {
      this.logWarning(`failed to close ${context}: ${this.formatError(error)}`);
    }
  }

  private logWarning(message: string): void {
    process.stderr.write(`[meridian-spawn-watch] warning: ${message}\n`);
  }

  private formatError(error: unknown): string {
    if (error instanceof Error) {
      const code = typeof (error as NodeJS.ErrnoException).code === "string" ? ` ${(error as NodeJS.ErrnoException).code}` : "";
      return `${error.name}${code}: ${error.message}`;
    }
    return String(error);
  }

  private requestScan(): void {
    if (this.scanScheduled) clearTimeout(this.scanScheduled);
    this.scanScheduled = setTimeout(() => {
      this.scanScheduled = null;
      void this.scan();
    }, DEBOUNCE_MS);
    this.scanScheduled.unref();
  }

  private async scan(): Promise<void> {
    if (this.scanRunning) {
      this.scanAgain = true;
      return;
    }
    this.scanRunning = true;
    try {
      do {
        this.scanAgain = false;
        await Promise.all([this.scanSpawns(), this.scanBashRecords()]);
      } while (this.scanAgain);
    } finally {
      this.scanRunning = false;
    }
  }

  private async scanSpawns(): Promise<void> {
    this.discoverMissingSpawnStates();
    const suppressed = await this.readSuppressedSpawnIds();
    const states = await this.rows();
    if (states.some((state) => !isTerminalSpawnStatus(state.status))) {
      this.enableFallbackScan("active-origin-spawns");
    } else {
      this.disableFallbackScan("active-origin-spawns");
    }
    for (const state of states) {
      if (!isTerminalSpawnStatus(state.status)) continue;
      const id = state.id;
      if (suppressed.has(id)) {
        TERMINAL_NOTIFIED.add(id);
        this.pending.delete(id);
        continue;
      }
      if (TERMINAL_NOTIFIED.has(id) || this.pending.has(id)) continue;
      this.pending.set(id, {
        id,
        kind: "spawn",
        status: String(state.status),
        label: `${state.agent ?? "spawn"}${state.model ? ` (${state.model})` : ""}`,
        duration: formatDurationSecs(state.duration_secs),
      });
    }
    this.scheduleFlush();
  }

  private async scanBashRecords(): Promise<void> {
    const file = await readJsonFile<BashRecordsFile | null>(this.bashRecordsPath, null);
    await this.rememberOriginBashIds(Object.keys(file?.records ?? {}));
    const bashIdsWithSpawns = new Set(
      (await this.readOriginSpawnStates(await this.readOriginBashIds()))
        .map((state) => state.originating_bash_id)
        .filter((id): id is string => typeof id === "string"),
    );
    for (const record of Object.values(file?.records ?? {})) {
      if (!record.is_tracked || !record.is_background || !isTerminalBashStatus(record.status)) {
        continue;
      }
      if (bashIdsWithSpawns.has(record.bash_id)) {
        TERMINAL_NOTIFIED.add(record.bash_id);
        this.pending.delete(record.bash_id);
        continue;
      }
      if (this.withinBashCorrelationGrace(record)) {
        this.scheduleBashGraceScan(record.bash_id);
        continue;
      }
      if (this.shouldDelayBashForDiscovery(record)) continue;
      if (TERMINAL_NOTIFIED.has(record.bash_id) || this.pending.has(record.bash_id)) continue;
      this.pending.set(record.bash_id, {
        id: record.bash_id,
        kind: "bash",
        status: record.status,
        label: record.command,
        duration: formatDurationSecs(((record.ended_at_ms ?? Date.now()) - record.started_at_ms) / 1000),
      });
    }
    this.scheduleFlush();
  }

  private withinBashCorrelationGrace(record: { ended_at_ms?: number | null }): boolean {
    const endedAt = typeof record.ended_at_ms === "number" ? record.ended_at_ms : null;
    return endedAt !== null && Date.now() - endedAt < BASH_SPAWN_CORRELATION_GRACE_MS;
  }

  private shouldDelayBashForDiscovery(record: { command?: string; ended_at_ms?: number | null }): boolean {
    const endedAt = typeof record.ended_at_ms === "number" ? record.ended_at_ms : null;
    return (
      endedAt !== null &&
      isSpawnLauncherCommand(String(record.command ?? "")) &&
      this.fallbackScanReasons.has("spawn-discovery") &&
      Date.now() - endedAt < SPAWN_DISCOVERY_POLL_MS
    );
  }

  private scheduleBashGraceScan(bashId: string): void {
    if (this.bashGraceTimers.has(bashId)) return;
    const timer = setTimeout(() => {
      this.bashGraceTimers.delete(bashId);
      this.requestScan();
    }, BASH_SPAWN_CORRELATION_GRACE_MS);
    timer.unref();
    this.bashGraceTimers.set(bashId, timer);
  }

  private scheduleFlush(): void {
    if (this.pending.size === 0) return;
    if (this.debounce) clearTimeout(this.debounce);
    this.debounce = setTimeout(() => void this.flush(), DEBOUNCE_MS);
    this.debounce.unref();
    if (!this.maxWave) {
      this.maxWave = setTimeout(() => void this.flush(), MAX_WAVE_WAIT_MS);
      this.maxWave.unref();
    }
  }

  private async flush(): Promise<void> {
    if (this.debounce) clearTimeout(this.debounce);
    if (this.maxWave) clearTimeout(this.maxWave);
    this.debounce = null;
    this.maxWave = null;
    const suppressed = await this.readSuppressedSpawnIds();
    const items = [...this.pending.values()].filter(
      (item) => item.kind !== "spawn" || !suppressed.has(item.id),
    );
    for (const item of this.pending.values()) {
      if (item.kind === "spawn" && suppressed.has(item.id)) TERMINAL_NOTIFIED.add(item.id);
    }
    this.pending.clear();
    if (items.length === 0) return;

    const content = await formatNotification(items);
    await this.pi.sendMessage?.(
      {
        customType: "meridian-spawn-watch",
        content,
        display: true,
        details: { ids: items.map((item) => item.id) },
      },
      { triggerTurn: true, deliverAs: "followUp" },
    );
    for (const item of items) TERMINAL_NOTIFIED.add(item.id);
    await writeJsonAtomic(this.markerPath, {
      ts_epoch_secs: Date.now() / 1000,
      notified_spawn_ids: items.filter((item) => item.kind === "spawn").map((item) => item.id),
    });
  }

  private isClearedTerminalSpawn(state: SpawnStateFile): boolean {
    return this.clearedSpawnIds.has(state.id) && isTerminalSpawnStatus(String(state.status ?? ""));
  }

  private async loadClearedSpawnIds(): Promise<void> {
    if (this.clearedLoaded) return;
    const file = await readJsonFile<ClearedSpawnsFile | null>(this.clearedPath, null);
    for (const id of file?.cleared_spawn_ids ?? []) {
      if (typeof id === "string") this.clearedSpawnIds.add(id);
    }
    this.clearedLoaded = true;
  }

  private async persistClearedSpawnIds(): Promise<void> {
    await writeJsonAtomic(this.clearedPath, {
      v: 1,
      spawn_id: this.currentSpawnId,
      updated_at_ms: Date.now(),
      cleared_spawn_ids: [...this.clearedSpawnIds].sort(),
    });
  }

  private async readSuppressedSpawnIds(): Promise<Set<string>> {
    const file = await readJsonFile<ObservedSpawnsFile | null>(this.observedPath, null);
    return new Set(
      [...(file?.observed_spawn_ids ?? []), ...(file?.waiting_spawn_ids ?? [])].filter(
        (id): id is string => typeof id === "string",
      ),
    );
  }

  private async readOriginSpawnStates(bashIds?: Set<string>): Promise<SpawnStateFile[]> {
    const originBashIds = bashIds ?? await this.readOriginBashIds();
    if (!existsSync(this.spawnsDir)) return [];
    const states: SpawnStateFile[] = [];
    for (const name of readdirSync(this.spawnsDir)) {
      if (!name.startsWith("p")) continue;
      const state = await this.readSpawnState(name);
      if (state) this.missingStateSpawnIds.delete(name);
      if (typeof state?.originating_bash_id === "string" && originBashIds.has(state.originating_bash_id)) {
        states.push(state);
      }
    }
    return states;
  }

  private async readOriginBashIds(): Promise<Set<string>> {
    await this.loadOriginBashIds();
    const file = await readJsonFile<BashRecordsFile | null>(this.bashRecordsPath, null);
    await this.rememberOriginBashIds(Object.keys(file?.records ?? {}));
    return new Set(this.originBashIds);
  }

  private async loadOriginBashIds(): Promise<void> {
    if (this.originsLoaded) return;
    for (const id of await readSpawnOriginBashIds(this.currentSpawnId)) this.originBashIds.add(id);
    this.originsLoaded = true;
  }

  private async rememberOriginBashIds(ids: string[]): Promise<void> {
    await this.loadOriginBashIds();
    for (const id of ids) {
      if (typeof id === "string" && id.length > 0) this.originBashIds.add(id);
    }
  }

  private async readSpawnState(spawnId: string): Promise<SpawnStateFile | null> {
    return await readJsonFile<SpawnStateFile | null>(this.spawnStatePath(spawnId), null);
  }

  private spawnStatePath(spawnId: string): string {
    return path.join(this.spawnsDir, spawnId, "state.json");
  }
}

async function formatNotification(items: NotificationItem[]): Promise<string> {
  const spawnIds = items.filter((item) => item.kind === "spawn").map((item) => item.id);
  const bashItems = items.filter((item) => item.kind === "bash");
  const sections: string[] = [];

  if (spawnIds.length > 0) {
    sections.push(await formatSpawnWaitNotification(spawnIds, items));
  }

  if (bashItems.length > 0) {
    sections.push(formatBashNotification(bashItems));
  }

  return sections.filter((section) => section.trim().length > 0).join("\n\n");
}

async function formatSpawnWaitNotification(
  spawnIds: string[],
  fallbackItems: NotificationItem[],
): Promise<string> {
  const result = await runMeridianCommand(["spawn", "wait", ...spawnIds], 30_000);
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

  if (status === "succeeded") return dim("✓ succeeded");
  if (status === "failed") return error("✗ failed");
  if (status === "cancelled" || status === "canceled") return warning(`✗ ${status}`);
  if (status === "timed_out") return error("✗ timed_out");
  if (!isTerminalSpawnStatus(status)) return success(`● ${status}`);
  return dim(status);
}

function renderSpawnPreview(row: SpawnStateFile, theme: Theme): string[] {
  const dim = (value: string) => theme.fg("dim", value);
  const lines = [
    `${theme.fg("accent", row.id)} ${formatSpawnStatus(row, theme)} ${dim(formatDurationSecs(row.duration_secs))}`,
    dim(`${row.agent ?? "spawn"}${row.model ? ` · ${row.model}` : ""}`),
  ];
  if (row.originating_bash_id) lines.push(dim(`launched by ${row.originating_bash_id}`));
  if (row.started_at) lines.push(dim(`started ${row.started_at}`));
  if (row.finished_at) lines.push(dim(`finished ${row.finished_at}`));
  return lines;
}

const SPAWN_PANEL_COLUMNS: SelectablePanelColumn<SpawnStateFile>[] = [
  { header: "ID", width: 10, render: (row, theme, selected) => (theme ? (selected ? theme.fg("accent", row.id) : theme.fg("dim", row.id)) : row.id) },
  { header: "STATUS", width: 14, render: (row, theme) => (theme ? formatSpawnStatus(row, theme) : String(row.status ?? "")) },
  { header: "DUR", width: 8, render: (row, theme) => (theme ? theme.fg("dim", formatDurationSecs(row.duration_secs)) : formatDurationSecs(row.duration_secs)), align: "right" },
  { header: "AGENT", width: 16, render: (row) => String(row.agent ?? "") },
  { header: "MODEL", width: 24, render: (row, theme) => (theme ? theme.fg("dim", String(row.model ?? "")) : String(row.model ?? "")) },
  { header: "← BASH", width: 10, render: (row, theme) => (theme ? theme.fg("dim", String(row.originating_bash_id ?? "")) : String(row.originating_bash_id ?? "")) },
];

export default function meridianSpawnWatchExtension(pi: ExtensionAPI): void {
  const runtime = new SpawnWatchRuntime(pi as PiWithMessages);
  pi.on?.("session_start", () => runtime.start());
  pi.on?.("session_shutdown", () => runtime.stop());

  pi.registerCommand("spawn", {
    description: "List Meridian spawns correlated to this Pi session.",
    handler: async (_args, ctx) => {
      const loadRows = async (): Promise<SpawnStateFile[]> => runtime.rows(true);

      if (ctx.hasUI === false || !ctx.ui?.custom) {
        const rows = await loadRows();
        const text = rows.length ? renderTable(SPAWN_PANEL_COLUMNS, rows, 100).join("\n") : "No correlated Meridian spawns.";
        process.stdout.write(`${text}\n`);
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
            initialFollow: !isTerminalSpawnStatus(String(row.status ?? "")),
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
