import { existsSync, mkdirSync, readdirSync, watch, type FSWatcher } from "node:fs";
import path from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Key, matchesKey } from "@earendil-works/pi-tui";

import { readJsonFile, writeJsonAtomic } from "../../shared/json_file";
import { runMeridianCommand } from "../../shared/meridian_cli";
import {
  currentSpawnIdFromEnv,
  resolveBashRecordsPath,
  resolveLastNotificationPath,
  resolvePiBashDir,
  resolveSpawnsDir,
} from "../../shared/pi_state_paths";
import type { BashRecordsFile, SpawnStateFile } from "../../shared/schemas";
import { isTerminalBashStatus, isTerminalSpawnStatus } from "../../shared/schemas";
import { formatDurationSecs, renderTable } from "../../shared/ui";

type PiWithMessages = ExtensionAPI & {
  sendMessage?: (
    message: { customType?: string; content: string; display?: boolean; details?: Record<string, unknown> },
    options?: { triggerTurn?: boolean; deliverAs?: "steer" | "followUp" | "nextTurn" },
  ) => void | Promise<void>;
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

class SpawnWatchRuntime {
  private readonly currentSpawnId = currentSpawnIdFromEnv();
  private readonly spawnsDir = resolveSpawnsDir();
  private readonly ownSpawnDir = path.join(this.spawnsDir, this.currentSpawnId);
  private readonly bashDir = resolvePiBashDir(this.currentSpawnId);
  private readonly bashRecordsPath = resolveBashRecordsPath(this.currentSpawnId);
  private readonly markerPath = resolveLastNotificationPath(this.currentSpawnId);
  private readonly pending = new Map<string, NotificationItem>();
  private readonly childSpawnIds = new Set<string>();
  private readonly childWatchers = new Map<string, FSWatcher>();
  private watchers: FSWatcher[] = [];
  private debounce: NodeJS.Timeout | null = null;
  private maxWave: NodeJS.Timeout | null = null;
  private scanScheduled: NodeJS.Timeout | null = null;
  private scanRunning = false;
  private scanAgain = false;

  constructor(private readonly pi: PiWithMessages) {}

  start(): void {
    mkdirSync(this.spawnsDir, { recursive: true });
    mkdirSync(this.ownSpawnDir, { recursive: true });
    mkdirSync(this.bashDir, { recursive: true });
    this.watchers.push(this.watchTopLevelSpawnsDir());
    this.watchers.push(this.watchDirectory(this.ownSpawnDir, () => this.requestScan()));
    this.watchers.push(this.watchDirectory(this.bashDir, () => this.requestScan()));
    void this.discoverExistingChildren().then(() => this.scan());
  }

  stop(): void {
    for (const watcher of this.watchers) watcher.close();
    this.watchers = [];
    for (const watcher of this.childWatchers.values()) watcher.close();
    this.childWatchers.clear();
    if (this.debounce) clearTimeout(this.debounce);
    if (this.maxWave) clearTimeout(this.maxWave);
    if (this.scanScheduled) clearTimeout(this.scanScheduled);
  }

  async rows(discover = false): Promise<SpawnStateFile[]> {
    if (discover) await this.discoverExistingChildren();
    return (await this.readChildSpawnStates()).filter(
      (state) => state.parent_id === this.currentSpawnId,
    );
  }

  private watchTopLevelSpawnsDir(): FSWatcher {
    return this.watchDirectory(this.spawnsDir, (_eventType, filename) => {
      const name = filename ? path.basename(filename.toString()) : "";
      if (name.startsWith("p")) {
        void this.discoverChild(name).then(() => this.requestScan());
        const retry = setTimeout(() => void this.discoverChild(name).then(() => this.requestScan()), 250);
        retry.unref();
        return;
      }
      this.requestScan();
    });
  }

  private watchChildSpawnDir(spawnId: string): void {
    if (this.childWatchers.has(spawnId)) return;
    const dir = path.join(this.spawnsDir, spawnId);
    if (!existsSync(dir)) return;
    const watcher = this.watchDirectory(dir, (_eventType, filename) => {
      const name = filename?.toString() ?? "";
      if (!name || name === "state.json") this.requestScan();
    });
    this.childWatchers.set(spawnId, watcher);
  }

  private watchDirectory(
    dir: string,
    onChange: (eventType: string, filename: string | Buffer | null) => void,
  ): FSWatcher {
    mkdirSync(dir, { recursive: true });
    const watcher = watch(dir, { recursive: false }, onChange);
    watcher.unref();
    return watcher;
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
    for (const state of await this.rows()) {
      if (!isTerminalSpawnStatus(state.status)) continue;
      const id = state.id;
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
    for (const record of Object.values(file?.records ?? {})) {
      if (!record.is_tracked || !record.is_background || !isTerminalBashStatus(record.status)) {
        continue;
      }
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
    const items = [...this.pending.values()];
    this.pending.clear();
    if (items.length === 0) return;

    const content = formatNotification(items);
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

  private async discoverExistingChildren(): Promise<void> {
    if (!existsSync(this.spawnsDir)) return;
    for (const name of readdirSync(this.spawnsDir)) {
      if (name.startsWith("p")) await this.discoverChild(name);
    }
  }

  private async discoverChild(spawnId: string): Promise<void> {
    if (this.childSpawnIds.has(spawnId)) {
      this.watchChildSpawnDir(spawnId);
      return;
    }
    const state = await this.readSpawnState(spawnId);
    if (state?.parent_id !== this.currentSpawnId) return;
    this.childSpawnIds.add(spawnId);
    this.watchChildSpawnDir(spawnId);
  }

  private async readChildSpawnStates(): Promise<SpawnStateFile[]> {
    const states: SpawnStateFile[] = [];
    for (const spawnId of this.childSpawnIds) {
      const state = await this.readSpawnState(spawnId);
      if (state) states.push(state);
    }
    return states;
  }

  private async readSpawnState(spawnId: string): Promise<SpawnStateFile | null> {
    const statePath = path.join(this.spawnsDir, spawnId, "state.json");
    return await readJsonFile<SpawnStateFile | null>(statePath, null);
  }
}

function formatNotification(items: NotificationItem[]): string {
  if (items.length === 1) {
    const item = items[0]!;
    return `${item.kind === "spawn" ? "Spawn" : "Background bash"} ${item.id} completed (${item.label}, ${item.duration}): ${item.status}\nUse \`${item.kind === "spawn" ? `meridian spawn show ${item.id}` : `bash_manage({action: "output", bash_id: "${item.id}"})`}\` for details.`;
  }
  return [
    "Background work completed:",
    ...items.map((item) => `- ${item.id} (${item.label}, ${item.duration}) ${item.status}`),
    "Use `meridian spawn show <id>` or `bash_manage(action='output')` for details.",
  ].join("\n");
}

export default function meridianSpawnWatchExtension(pi: ExtensionAPI): void {
  const runtime = new SpawnWatchRuntime(pi as PiWithMessages);
  pi.on?.("session_start", () => runtime.start());
  pi.on?.("session_shutdown", () => runtime.stop());

  pi.registerCommand("mspawn", {
    description: "List Meridian spawns correlated to this Pi session.",
    handler: async (_args, ctx) => {
      const rows = await runtime.rows(true);
      const columns = [
        { header: "ID", width: 10, render: (row: SpawnStateFile) => row.id },
        { header: "STATUS", width: 12, render: (row: SpawnStateFile) => String(row.status ?? "") },
        { header: "AGENT", width: 16, render: (row: SpawnStateFile) => String(row.agent ?? "") },
        { header: "MODEL", width: 24, render: (row: SpawnStateFile) => String(row.model ?? "") },
        { header: "← BASH", width: 10, render: (row: SpawnStateFile) => String(row.originating_bash_id ?? "") },
      ];
      await ctx.ui.custom(
        (_tui, theme, _keybindings, done) => ({
          render(width: number): string[] {
            const title = theme.fg("accent", theme.bold(" Meridian /mspawn — correlated spawns "));
            const body = rows.length ? renderTable(columns, rows, Math.max(20, width - 4)) : ["No correlated Meridian spawns."];
            return [
              theme.fg("border", "╭" + "─".repeat(Math.max(0, width - 2)) + "╮"),
              theme.fg("border", "│ ") + title,
              theme.fg("border", "├" + "─".repeat(Math.max(0, width - 2)) + "┤"),
              ...body.map((line) => theme.fg("border", "│ ") + line),
              theme.fg("border", "├" + "─".repeat(Math.max(0, width - 2)) + "┤"),
              theme.fg("dim", "│ q/esc close • /mspawn:show <id> • /mspawn:log <id>"),
              theme.fg("border", "╰" + "─".repeat(Math.max(0, width - 2)) + "╯"),
            ];
          },
          handleInput(data: string): void {
            if (matchesKey(data, Key.escape) || data === "q") done(undefined);
          },
          invalidate(): void {},
        }),
        { overlay: true, overlayOptions: { width: "90%", maxHeight: "80%" } },
      );
    },
  });

  pi.registerCommand("mspawn:show", {
    description: "Show a correlated Meridian spawn.",
    handler: async (args, ctx) => {
      const result = await runMeridianCommand(["spawn", "show", args.trim()], 15_000);
      ctx.ui.notify(result.stdout || result.stderr, result.exitCode === 0 ? "info" : "error");
    },
  });

  pi.registerCommand("mspawn:wait", {
    description: "Wait for a correlated Meridian spawn.",
    handler: async (args, ctx) => {
      const result = await runMeridianCommand(["spawn", "wait", args.trim()], 60_000);
      ctx.ui.notify(result.stdout || result.stderr, result.exitCode === 0 ? "info" : "error");
    },
  });

  pi.registerCommand("mspawn:cancel", {
    description: "Cancel a correlated Meridian spawn.",
    handler: async (args, ctx) => {
      const result = await runMeridianCommand(["spawn", "cancel", args.trim()], 15_000);
      ctx.ui.notify(result.stdout || result.stderr, result.exitCode === 0 ? "info" : "error");
    },
  });

  pi.registerCommand("mspawn:log", {
    description: "Show recent log for a correlated Meridian spawn.",
    handler: async (args, ctx) => {
      const result = await runMeridianCommand(["session", "log", args.trim()], 15_000);
      ctx.ui.notify(result.stdout || result.stderr, result.exitCode === 0 ? "info" : "error");
    },
  });
}
