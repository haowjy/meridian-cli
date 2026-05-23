import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

import { LogDockComponent } from "../components/log-dock-component";
import { panelConfig } from "../panel/config";
import { formatPingBadge } from "../panel/ping_format";
import { getForegroundUserBashTaskId, USER_BASH_FOREGROUND_HINT } from "../bash_bridge";
import type { TaskPanelHost } from "../panel/host";
import type { PanelEntry } from "../panel/types";
import { formatDurationMs } from "../panel/ping_format";
import {
  resolveSpawnTaskPingDefaults,
  TASK_PING_INTERVAL_MS_ENV,
  TASK_PING_RESET_ON_ACTIVITY_ENV,
} from "../session_ping";

export type DockActions = {
  getFocusedProcessId: () => string | null;
  setFocus: (id: string | null) => void;
  expand: () => void;
  hide: () => void;
  toggle: () => void;
};

const STATUS_WIDGET_ID = "meridian-background-tasks:status";
const LOG_DOCK_WIDGET_ID = "meridian-background-tasks:log-dock";

function formatEntryStatus(
  entry: PanelEntry,
  theme: ExtensionContext["ui"]["theme"],
): string {
  const name = entry.name.length > 18 ? `${entry.name.slice(0, 15)}...` : entry.name;
  const ping = formatPingBadge(entry);
  const pingSuffix = ping ? ` ${theme.fg("dim", ping)}` : "";
  if (entry.isLive) {
    return `${theme.fg("accent", name)} ${theme.fg("dim", "running")}${pingSuffix}`;
  }
  if (entry.success === false) {
    return `${theme.fg("error", name)} ${theme.fg("error", entry.status)}${pingSuffix}`;
  }
  return `${theme.fg("dim", name)} ${theme.fg("dim", "done")}${pingSuffix}`;
}

function renderStatusWidget(
  entries: PanelEntry[],
  theme: ExtensionContext["ui"]["theme"],
  maxWidth?: number,
): string[] {
  if (entries.length === 0) {
    return [];
  }
  const live = entries.filter((entry) => entry.isLive);
  const finished = entries.filter((entry) => !entry.isLive);
  const ordered = [...live, ...finished];
  const prefix = theme.fg("dim", "tasks: ");
  const effectiveMax = maxWidth ?? 200;
  const parts: string[] = [];
  let used = prefix.length;
  for (const entry of ordered) {
    const formatted = formatEntryStatus(entry, theme);
    const needed = (parts.length > 0 ? 3 : 0) + formatted.length;
    if (used + needed > effectiveMax && parts.length > 0) {
      parts.push(theme.fg("dim", `+${ordered.length - parts.length} more`));
      break;
    }
    parts.push(formatted);
    used += needed;
  }
  if (parts.length === 0) {
    return [];
  }
  return [prefix + parts.join(theme.fg("dim", " | "))];
}

export function setupTaskWidget(
  pi: ExtensionAPI,
  host: TaskPanelHost,
): { update: () => Promise<void>; dockActions: DockActions } {
  let activeCtx: ExtensionContext | null = null;
  let logDockComponent: LogDockComponent | null = null;
  let logDockComponentTui: { requestRender: () => void } | null = null;

  const dockState = {
    visibility: panelConfig.widget.dockDefaultState,
    focusedProcessId: null as string | null,
  };

  const dockActions: DockActions = {
    getFocusedProcessId: () => dockState.focusedProcessId,
    setFocus(id) {
      dockState.focusedProcessId = id;
      if (id && dockState.visibility === "hidden") {
        dockState.visibility = "open";
      }
      void updateWidget();
    },
    expand() {
      dockState.visibility = "open";
      void updateWidget();
    },
    hide() {
      dockState.visibility = "hidden";
      void updateWidget();
    },
    toggle() {
      dockState.visibility = dockState.visibility === "hidden" ? "collapsed" : "hidden";
      void updateWidget();
    },
  };

  async function updateWidget(): Promise<void> {
    if (!activeCtx?.hasUI) {
      return;
    }
    const entries = await host.list();
    host.setSyncEntries(entries);

    if (panelConfig.widget.showStatusWidget) {
      const maxWidth = process.stdout.columns || 120;
      const lines = renderStatusWidget(entries, activeCtx.ui.theme, maxWidth);
      const pingDefaults = resolveSpawnTaskPingDefaults();
      const sessionPing =
        pingDefaults.pingIntervalMs != null
          ? `session ping ${formatDurationMs(pingDefaults.pingIntervalMs)}`
          : "session ping default 55m";
      const resetLabel = pingDefaults.pingResetOnActivity ? "reset on activity" : "no activity reset";
      const policyLine = themeLine(
        activeCtx.ui.theme,
        `${sessionPing} · ${resetLabel}`,
      );
      const foregroundHint = getForegroundUserBashTaskId()
        ? themeLine(activeCtx.ui.theme, USER_BASH_FOREGROUND_HINT)
        : null;
      const widgetLines =
        lines.length > 0
          ? [...lines, policyLine, ...(foregroundHint ? [foregroundHint] : [])]
          : foregroundHint
            ? [foregroundHint, policyLine]
            : entries.length > 0
              ? [policyLine]
              : [];
      activeCtx.ui.setWidget(
        STATUS_WIDGET_ID,
        widgetLines.length > 0 ? widgetLines : undefined,
        { placement: "belowEditor" },
      );
    }

    if (dockState.visibility === "hidden") {
      activeCtx.ui.setWidget(LOG_DOCK_WIDGET_ID, undefined);
      logDockComponent?.dispose();
      logDockComponent = null;
      logDockComponentTui = null;
      return;
    }

    const mode = dockState.visibility === "open" ? "open" : "collapsed";
    const height = mode === "collapsed" ? 3 : panelConfig.widget.dockHeight;
    const ctx = activeCtx;

    if (logDockComponent && logDockComponentTui) {
      logDockComponent.update({
        mode,
        focusedProcessId: dockState.focusedProcessId,
        dockHeight: height,
      });
      return;
    }

    ctx.ui.setWidget(
      LOG_DOCK_WIDGET_ID,
      (tui, theme) => {
        logDockComponent = new LogDockComponent({
          host,
          tui,
          theme,
          mode,
          focusedProcessId: dockState.focusedProcessId,
          dockHeight: height,
        });
        logDockComponentTui = tui;
        return logDockComponent;
      },
      { placement: "aboveEditor" },
    );
  }

  pi.on("session_start", async (_event, ctx) => {
    activeCtx = ctx;
    await updateWidget();
  });

  pi.on("session_shutdown", async () => {
    activeCtx = null;
    logDockComponent?.dispose();
    logDockComponent = null;
    logDockComponentTui = null;
  });

  host.onEvent(() => {
    void updateWidget();
  });

  return { update: updateWidget, dockActions };
}

function themeLine(theme: ExtensionContext["ui"]["theme"], text: string): string {
  return theme.fg("dim", text);
}

export function formatSettingsPingSummary(): string {
  const defaults = resolveSpawnTaskPingDefaults();
  const lines = [
    `MERIDIAN_PI_TASK_PING_INTERVAL_MS=${process.env[TASK_PING_INTERVAL_MS_ENV] ?? "(unset → 55m fallback)"}`,
    `MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY=${process.env[TASK_PING_RESET_ON_ACTIVITY_ENV] ?? "(unset → true)"}`,
    `effective session default: ${
      defaults.pingIntervalMs != null
        ? formatDurationMs(defaults.pingIntervalMs)
        : "55m fallback"
    }`,
    `reset on activity: ${defaults.pingResetOnActivity}`,
    `default persistent: ${defaults.defaultPersistent}`,
  ];
  return lines.join("\n");
}
