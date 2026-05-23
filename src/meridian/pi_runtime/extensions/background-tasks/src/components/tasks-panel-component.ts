import type { Theme } from "@earendil-works/pi-coding-agent";
import type { Component } from "@earendil-works/pi-tui";

import { backgroundForegroundBash } from "../background_foreground";
import {
  isPanelBackground,
  isPanelClear,
  isPanelConfirm,
  isPanelDown,
  isPanelKill,
  isPanelLogScrollDown,
  isPanelLogScrollUp,
  isPanelQuit,
  isPanelUp,
} from "../panel/input";
import { computePanelLayout } from "../panel/layout";
import type { PsPanelActions } from "../panel/ps-view";
import { renderPsPanel } from "../panel/ps-view";
import type { TaskPanelHost } from "../panel/host";
import type { PanelEntry } from "../panel/types";

export type { PsPanelActions } from "../panel/ps-view";

type PsPanelTui = {
  requestRender: () => void;
  terminal?: { rows?: number };
};

const LIVE_REFRESH_MS = 1000;

/**
 * /ps overlay component. Implements Pi `Component` directly — do not wrap in a
 * Container or call `tui.setFocus`; Pi's `showOverlay` owns focus/preFocus.
 */
export class TasksPanelComponent implements Component {
  private readonly tui: PsPanelTui;
  private readonly theme: Theme;
  private readonly actions: PsPanelActions;
  private readonly host: TaskPanelHost;

  private entries: PanelEntry[] = [];
  private selectedIndex = 0;
  private processScrollOffset = 0;
  private logScrollOffset = 0;
  private readonly logScroll = { above: 0, below: 0 };
  private cachedLines: string[] = [];
  private cachedWidth = 0;
  private unsubscribe: (() => void) | null = null;
  private liveRefreshTimer: ReturnType<typeof setInterval> | null = null;
  private backgroundingForeground = false;
  private closed = false;

  constructor(
    tui: PsPanelTui,
    theme: Theme,
    actions: PsPanelActions,
    host: TaskPanelHost,
  ) {
    this.tui = tui;
    this.theme = theme;
    this.actions = actions;
    this.host = host;

    void this.refreshEntries();
    this.unsubscribe = this.host.onEvent(() => {
      void this.refreshEntries();
    });

    this.liveRefreshTimer = setInterval(() => {
      if (this.closed || !this.entries.some((entry) => entry.isLive)) {
        return;
      }
      this.invalidate();
      this.tui.requestRender();
    }, LIVE_REFRESH_MS);
    this.liveRefreshTimer.unref?.();
  }

  dispose(): void {
    this.closed = true;
    if (this.liveRefreshTimer != null) {
      clearInterval(this.liveRefreshTimer);
      this.liveRefreshTimer = null;
    }
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  invalidate(): void {
    this.cachedWidth = 0;
    this.cachedLines = [];
  }

  private async refreshEntries(): Promise<void> {
    if (this.closed) {
      return;
    }
    this.entries = await this.host.list();
    this.host.setSyncEntries(this.entries);
    if (this.selectedIndex >= this.entries.length) {
      this.selectedIndex = Math.max(0, this.entries.length - 1);
    }
    this.ensureProcessVisible(this.entries.length);
    this.invalidate();
    if (!this.closed) {
      this.tui.requestRender();
    }
  }

  handleInput(data: string): void {
    if (this.closed) {
      return;
    }
    const processes = this.entries;

    if (isPanelQuit(data)) {
      this.actions.onQuit();
      return;
    }

    if (isPanelDown(data)) {
      if (processes.length > 0) {
        this.selectedIndex = Math.min(this.selectedIndex + 1, processes.length - 1);
        this.logScrollOffset = 0;
        this.ensureProcessVisible(processes.length);
        this.invalidate();
        this.tui.requestRender();
      }
      return;
    }

    if (isPanelUp(data)) {
      if (processes.length > 0) {
        this.selectedIndex = Math.max(this.selectedIndex - 1, 0);
        this.logScrollOffset = 0;
        this.ensureProcessVisible(processes.length);
        this.invalidate();
        this.tui.requestRender();
      }
      return;
    }

    if (isPanelLogScrollUp(data)) {
      this.logScrollOffset = Math.max(0, this.logScrollOffset - 5);
      this.invalidate();
      this.tui.requestRender();
      return;
    }

    if (isPanelLogScrollDown(data)) {
      this.logScrollOffset += 5;
      this.invalidate();
      this.tui.requestRender();
      return;
    }

    if (isPanelConfirm(data)) {
      const proc = processes[this.selectedIndex];
      if (proc) {
        void this.actions.onOpenStream(proc.id);
      }
      return;
    }

    if (isPanelKill(data)) {
      const proc = processes[this.selectedIndex];
      if (proc?.isLive) {
        void this.host.kill(proc.id).then(() => this.refreshEntries());
      }
      return;
    }

    if (isPanelClear(data)) {
      void this.host.clearFinished().then(() => this.refreshEntries());
      return;
    }

    if (isPanelBackground(data)) {
      const proc = processes[this.selectedIndex];
      if (proc?.isForeground && !this.backgroundingForeground) {
        this.backgroundingForeground = true;
        void backgroundForegroundBash(this.host).finally(() => {
          this.backgroundingForeground = false;
          setTimeout(() => {
            void this.refreshEntries();
          }, 0);
        });
      }
    }
  }

  private ensureProcessVisible(totalProcesses: number): void {
    const terminalRows = this.tui.terminal?.rows ?? 24;
    const maxVisibleProcesses = computePanelLayout(terminalRows, totalProcesses).maxVisibleProcesses;
    const visibleCount = Math.min(maxVisibleProcesses, totalProcesses);
    if (this.selectedIndex < this.processScrollOffset) {
      this.processScrollOffset = this.selectedIndex;
    } else if (this.selectedIndex >= this.processScrollOffset + visibleCount) {
      this.processScrollOffset = this.selectedIndex - visibleCount + 1;
    }
    this.processScrollOffset = Math.max(
      0,
      Math.min(this.processScrollOffset, Math.max(0, totalProcesses - visibleCount)),
    );
  }

  render(width: number): string[] {
    if (width === this.cachedWidth && this.cachedLines.length > 0) {
      return this.cachedLines;
    }

    this.cachedLines = renderPsPanel(
      width,
      this.tui.terminal?.rows ?? 24,
      this.theme,
      this.host,
      {
        entries: this.entries,
        selectedIndex: this.selectedIndex,
        processScrollOffset: this.processScrollOffset,
        logScrollOffset: this.logScrollOffset,
        backgroundingForeground: this.backgroundingForeground,
        logScroll: this.logScroll,
      },
    );
    this.cachedWidth = width;
    return this.cachedLines;
  }
}
