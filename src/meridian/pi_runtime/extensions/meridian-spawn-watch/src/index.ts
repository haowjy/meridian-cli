import { existsSync, mkdirSync, readdirSync, watch, type FSWatcher } from "node:fs";
import path from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

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
  private readonly bashDir = resolvePiBashDir(this.currentSpawnId);
  private readonly bashRecordsPath = resolveBashRecordsPath(this.currentSpawnId);
  private readonly markerPath = resolveLastNotificationPath(this.currentSpawnId);
  private readonly pending = new Map<string, NotificationItem>();
  private watchers: FSWatcher[] = [];
  private debounce: NodeJS.Timeout | null = null;
  private maxWave: NodeJS.Timeout | null = null;

  constructor(private readonly pi: PiWithMessages) {}

  start(): void {
    this.watchers.push(this.watchDirectory(this.spawnsDir));
    this.watchers.push(this.watchDirectory(this.bashDir));
    void this.scan();
  }

  stop(): void {
    for (const watcher of this.watchers) watcher.close();
    this.watchers = [];
    if (this.debounce) clearTimeout(this.debounce);
    if (this.maxWave) clearTimeout(this.maxWave);
  }

  async rows(): Promise<SpawnStateFile[]> {
    const bashIds = await this.sessionBashIds();
    const rows: SpawnStateFile[] = [];
    for (const state of await this.readSpawnStates()) {
      if (state.originating_bash_id && bashIds.has(state.originating_bash_id)) {
        rows.push(state);
      }
    }
    return rows;
  }

  private watchDirectory(dir: string): FSWatcher {
    mkdirSync(dir, { recursive: true });
    return watch(dir, { recursive: true }, () => void this.scan());
  }

  private async scan(): Promise<void> {
    await Promise.all([this.scanSpawns(), this.scanBashRecords()]);
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
    this.maxWave ??= setTimeout(() => void this.flush(), MAX_WAVE_WAIT_MS);
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

  private async sessionBashIds(): Promise<Set<string>> {
    const file = await readJsonFile<BashRecordsFile | null>(this.bashRecordsPath, null);
    return new Set(Object.keys(file?.records ?? {}));
  }

  private async readSpawnStates(): Promise<SpawnStateFile[]> {
    if (!existsSync(this.spawnsDir)) return [];
    const states: SpawnStateFile[] = [];
    for (const name of readdirSync(this.spawnsDir)) {
      const statePath = path.join(this.spawnsDir, name, "state.json");
      const state = await readJsonFile<SpawnStateFile | null>(statePath, null);
      if (state) states.push(state);
    }
    return states;
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
  runtime.start();

  pi.registerCommand("mspawn", {
    description: "List Meridian spawns correlated to this Pi session.",
    handler: async (_args, ctx) => {
      const rows = await runtime.rows();
      const lines = rows.length
        ? renderTable(
            [
              { header: "ID", width: 10, render: (row) => row.id },
              { header: "STATUS", width: 12, render: (row) => String(row.status ?? "") },
              { header: "AGENT", width: 16, render: (row) => String(row.agent ?? "") },
              { header: "MODEL", width: 24, render: (row) => String(row.model ?? "") },
              { header: "← BASH", width: 10, render: (row) => String(row.originating_bash_id ?? "") },
            ],
            rows,
            100,
          )
        : ["No correlated Meridian spawns."];
      ctx.ui.notify(lines.join("\n"), "info");
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
